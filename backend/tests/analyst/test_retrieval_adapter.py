from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import unittest

from app.analyst.analyst import cache_key, loose_key
from app.analyst.batch_adapter import BatchValidationError
from app.analyst.input_adapter import stable_hash
from app.analyst.qa import validate_draft
from app.analyst.retrieval_adapter import adapt_retrieval_batch, text_hash
from app.analyst.schemas import LLMDraft
from app.analyst.view import build_prompt_view
from tests.analyst.helpers import draft, make_service
from tests.analyst.retrieval_fixtures import batch, contexts, merged, raw, source
from tests.analyst.test_handoff8 import make_batch


class RetrievalAdapterTests(unittest.TestCase):
    def adapt(self, b=None, *, profile=None, interests=None, policy=None):
        p, i = contexts()
        return adapt_retrieval_batch(b or batch(), p if profile is None else profile,
                                     i if interests is None else interests, "cfo", policy)

    def test_raw_exact_quotes_ids_coordinates_and_sources(self):
        b = batch()
        r = self.adapt(b)
        c = r.analyst_input.candidates[0]
        projection = c.analysis_projection.event_bundles[0]
        e = projection.evidence[0]
        self.assertEqual(e.quote, b["sources"][0]["text"][e.start:e.end])
        self.assertEqual(e.source_id, b["sources"][0]["revision_id"])
        self.assertEqual(projection.claims[0].evidence_ids, [e.evidence_id])
        self.assertEqual(projection.sources[0].url, b["sources"][0]["url"])
        self.assertEqual(c.canonical_event_id, "article:synthetic:article-a")
        self.assertEqual(c.reading_group_ids, ["reading-group"])
        self.assertEqual(projection.event.text_version, "1")
        self.assertEqual(projection.event.text_sha256, text_hash(b["candidates"][0]["item"]["text"]))

    def test_raw_fragmentation_is_exact_and_complete_unicode(self):
        s = source("long", "😀я"*800)
        c = self.adapt(batch([raw(s)], [s])).analyst_input.candidates[0]
        evs = c.analysis_projection.event_bundles[0].evidence
        self.assertEqual("".join(e.quote for e in evs), s["text"])
        for e in evs:
            self.assertEqual(e.quote, s["text"][e.start:e.end])

    def test_facts_and_hypotheses_are_separate_no_vectors_no_cosine_prior(self):
        r = self.adapt()
        c = r.analyst_input.candidates[0]
        view = build_prompt_view(r.analyst_input, c)
        data = json.dumps(view.data, ensure_ascii=False)
        self.assertNotIn('"vector"', data)
        self.assertNotIn('embedding_text', data)
        self.assertNotIn('0.8', data)
        self.assertIsNone(c.match_context.prior_score)
        self.assertEqual(len(r.analyst_input.company_context.verified_facts), 1)
        self.assertIn("Поисковая гипотеза", view.data["selection_signals"]["hypotheses"][0])
        self.assertNotIn("energy", [f.feature_id for f in r.analyst_input.company_context.verified_facts])

    def test_derived_company_feature_does_not_become_verified(self):
        p, _ = contexts()
        p["features"]["SYNTHETIC:/okved/name"]["quality"] = "derived"
        r = self.adapt(profile=p)
        self.assertFalse(r.analyst_input.company_context.verified_facts)
        self.assertTrue(r.analyst_input.company_context.unknowns)

    def test_merged_event_uses_original_quotes_and_derived_context(self):
        a, b = source("a", "СИНТЕТИКА: тариф повышен."), source("b", "СИНТЕТИКА: сообщается о повышении тарифа.")
        candidate = merged([a, b])
        r = self.adapt(batch([candidate], [a,b]))
        c = r.analyst_input.candidates[0]
        p = c.analysis_projection.event_bundles[0]
        self.assertEqual({e.quote for e in p.evidence}, {a["text"], b["text"]})
        summary = candidate["item"]["text"]
        self.assertNotIn(summary, [e.quote for e in p.evidence])
        self.assertNotIn(summary, [claim.text for claim in p.claims])
        self.assertFalse(p.derived_context["independent_evidence"])
        view = build_prompt_view(r.analyst_input, c)
        self.assertEqual(view.data["event"]["derived_context"][0]["text"], summary)
        self.assertEqual(len(view.data["event"]["original_quotes"]), 2)
        self.assertEqual(p.event.text_version, candidate["item"]["item_version"])

    def test_qa_cannot_resolve_derived_summary_as_independent_evidence(self):
        a = source("a", "СИНТЕТИКА: тариф повышен.")
        r = self.adapt(batch([merged([a])], [a]))
        view = build_prompt_view(r.analyst_input, r.analyst_input.candidates[0])
        model = LLMDraft.model_validate(draft(what_happened=[dict(text="Производный пересказ", kind="fact", claim_ids=["derived_merge"])]))
        qa = validate_draft(model, view, "cfo", as_of=r.analyst_input.request.as_of)
        self.assertNotIn("Производный пересказ", [f.text for f in qa.facts])
        self.assertEqual(qa.facts[0].evidence_ids, [next(iter(view.evidence))])
        self.assertTrue(any(issue.code == "fact_without_source" for issue in qa.issues))

    def test_split_events_in_same_group_survive_service_dedup(self):
        a = source("a", "СИНТЕТИКА: тариф повышен. СИНТЕТИКА: экспортная пошлина отменена.")
        cut = a["text"].index(" СИНТЕТИКА")
        x = merged([a], "event-a", slices={"a":(0,cut)})
        y = merged([a], "event-b", slices={"a":(cut,len(a["text"]))})
        service, fake, _ = make_service()
        p, i = contexts()
        output, r = service.analyze_batch(batch([x,y], [a]), "cfo", company_profile=p, active_interests=i)
        self.assertEqual(len(output.ranked_insights), 2)
        self.assertEqual(len({c.candidate_id for c in r.analyst_input.candidates}), 2)
        self.assertEqual([c.reading_group_ids for c in r.analyst_input.candidates], [["reading-group"]]*2)
        self.assertFalse([c for c in output.excluded_candidates if c.reason_code == "duplicate"])
        self.assertEqual(fake.stats.calls, 2)

    def test_repeat_sources_and_candidates_deduplicated(self):
        b = batch()
        b["sources"].append(copy.deepcopy(b["sources"][0]))
        b["candidates"].append(copy.deepcopy(b["candidates"][0]))
        r = self.adapt(b)
        self.assertEqual(len(r.analyst_input.candidates), 1)
        self.assertIn("duplicate_source_revision:article-a", r.notes)
        self.assertEqual(r.candidates[1]["state"], "duplicate")

    def test_conflicting_duplicate_source_is_error(self):
        b = batch()
        b["sources"].append(dict(b["sources"][0], url="https://example.org/other"))
        with self.assertRaisesRegex(BatchValidationError, "conflicting_source"):
            self.adapt(b)

    def test_duplicate_source_reference_is_rejected(self):
        b = batch()
        b["candidates"][0]["item"]["source_revision_ids"] *= 2
        self.assert_invalid(self.adapt(b), "duplicate_source_revision_reference")

    def test_missing_unknown_and_day_dates_preserved_without_merge_timestamp(self):
        for published, precision in [(None,"unknown"),("2026-10-23","day")]:
            with self.subTest(precision=precision):
                s = source("a", "СИНТЕТИКА: сообщение.", published_at=published, precision=precision)
                r = self.adapt(batch([merged([s])],[s]))
                c = r.analyst_input.candidates[0]
                p = c.analysis_projection.event_bundles[0]
                self.assertIsNone(p.event.first_published_at)
                v = build_prompt_view(r.analyst_input, c).data["event"]
                self.assertEqual(v["sources"][0]["published_at"], published)
                self.assertEqual(v["sources"][0]["publication_precision"], precision)
                self.assertIsNone(v["published_at"])
        b = batch()
        del b["sources"][0]["published_at"]
        del b["sources"][0]["publication_precision"]
        self.assertIsNone(self.adapt(b).analyst_input.candidates[0].analysis_projection.event_bundles[0].event.first_published_at)

    def test_timestamp_timezone_and_url_are_preserved(self):
        b = batch()
        b["sources"][0]["published_at"] = "2026-10-23T09:34:56+05:30"
        r = self.adapt(b)
        c = r.analyst_input.candidates[0]
        s = build_prompt_view(r.analyst_input,c).data["event"]["sources"][0]
        self.assertEqual(s["published_at"], b["sources"][0]["published_at"])
        self.assertEqual(s["url"], b["sources"][0]["url"])

    def test_invalid_dates_hashes_and_urls_fail_package(self):
        for field, value in [("published_at","yesterday"),("published_at","2026-10-23T12:00:00"),
                             ("text_sha256",""),("text_sha256","0"*64),("url","file:///private"),
                             ("observed_at","2026-10-23")]:
            with self.subTest(field=field,value=value):
                b = batch(); b["sources"][0][field] = value
                with self.assertRaises(BatchValidationError):
                    self.adapt(b)
        b = batch();b["sources"][0]["publication_precision"] = "unknown"
        with self.assertRaises(BatchValidationError):self.adapt(b)

    def test_future_publication_and_observation_reject_candidate(self):
        for field in ["published_at","observed_at"]:
            b = batch(); b["sources"][0][field] = "2026-10-25T12:00:00+00:00"
            self.assert_invalid(self.adapt(b), "after_as_of")

    def test_partial_deferred_and_unresolved_are_explicit(self):
        b = batch();b["status"] = "partial";b["stop_reason"] = "merge_operation_limit"
        b["deferred"] = [dict(cluster_id="other",reason="unresolved_repartition")]
        b["candidates"][0]["processing_status"] = "unresolved"
        r = self.adapt(b)
        self.assertEqual(r.status,"partial")
        self.assertEqual(r.deferred, b["deferred"])
        self.assertEqual(r.candidates[0]["state"],"unresolved")
        self.assertFalse(r.analyst_input.candidates)
        service, fake, _ = make_service()
        p,i = contexts()
        out,_ = service.analyze_batch(b,"cfo",company_profile=p,active_interests=i)
        self.assertEqual(out.run_metadata.input_diagnostics["deferred"], b["deferred"])
        self.assertFalse(out.excluded_candidates)
        self.assertEqual(fake.stats.calls,0)

    def test_merge_fallback_can_analyze_original_not_become_irrelevant(self):
        b = batch();b["status"]="partial"
        b["candidates"][0].update(processing_status="merge_fallback",merge_fallback=[dict(reason="merge_service_unavailable")])
        r = self.adapt(b)
        self.assertEqual(r.candidates[0]["state"],"merge_fallback")
        self.assertEqual(len(r.analyst_input.candidates),1)

    def test_company_profile_interest_versions_and_feature_ids_must_match(self):
        for container,key in [("profile","company_id"),("profile","profile_version"),("interests","company_id"),
                              ("interests","profile_version"),("interests","interest_set_version"),("interests","encoder_fingerprint")]:
            p,i = contexts();(p if container=="profile" else i)[key]="other"
            with self.subTest(container=container,key=key), self.assertRaises(BatchValidationError):
                self.adapt(profile=p,interests=i)
        p,i = contexts();p["features"]["SYNTHETIC:/okved/name"]["feature_id"]="OTHER:/okved/name"
        with self.assertRaises(BatchValidationError):self.adapt(profile=p)

    def test_inactive_unknown_interests_and_nonfinite_cosine_rejected(self):
        for value in ["missing", "inactive", "nonfinite"]:
            b = batch();p,i=contexts()
            if value=="missing":b["candidates"][0]["best_interest_id"]="absent"
            elif value=="inactive":i["interests"][0]["active"]=False
            else:b["candidates"][0]["embedding_score"]=float("nan")
            self.assert_invalid(self.adapt(b,interests=i))

    def test_broken_source_fragment_quote_coordinates_and_paragraph_links(self):
        a=source("a","СИНТЕТИКА: тариф повышен.")
        for mutation in ["source","quote","coordinates","paragraph","title","membership"]:
            b=batch([merged([a])],[a]);item=b["candidates"][0]["item"];e=item["derived_from"]["event"]
            if mutation=="source":item["source_revision_ids"]=["missing"]
            elif mutation=="quote":e["fragments"][0]["text"]="Выдумка"
            elif mutation=="coordinates":e["fragments"][0]["end"]+=1
            elif mutation=="paragraph":e["paragraphs"][0]["fragment_ids"]=["missing"]
            elif mutation=="title":e["title_fragment_ids"]=["missing"]
            else:e["article_ids"]=["missing"]
            item["item_version"]=stable_hash(e)
            with self.subTest(mutation=mutation):self.assert_invalid(self.adapt(b))

    def test_event_version_and_raw_content_cannot_be_spoofed(self):
        a=source("a","СИНТЕТИКА: тариф повышен.")
        b=batch([merged([a])],[a]);b["candidates"][0]["item"]["item_version"]="wrong"
        self.assert_invalid(self.adapt(b),"event_version_hash_mismatch")
        b=batch();b["candidates"][0]["item"]["text"]="Invented"
        self.assert_invalid(self.adapt(b),"article_source_content_mismatch")

    def test_context_overflow_is_explicit_and_never_calls_llm(self):
        for policy in [dict(max_input_tokens=1),dict(max_projection_chars=10)]:
            service,fake,_=make_service();p,i=contexts()
            out,r=service.analyze_batch(batch(),"cfo",company_profile=p,active_interests=i,analysis_policy=policy)
            self.assertEqual(r.candidates[0]["state"],"context_too_large")
            self.assertEqual(r.candidates[0]["omitted_source_ids"],["article-a"])
            self.assertFalse(r.analyst_input.candidates)
            self.assertEqual(fake.stats.calls,0)
            self.assertFalse(out.excluded_candidates)

    def test_bad_candidate_does_not_poison_good_candidate(self):
        b=batch();bad=copy.deepcopy(b["candidates"][0]);bad["item"]["source_revision_ids"]=["missing"]
        b["candidates"].append(bad)
        r=self.adapt(b)
        self.assertEqual(len(r.analyst_input.candidates),1)
        self.assertEqual(r.candidates[1]["state"],"invalid")
        self.assertEqual(r.status,"partial")

    def test_strict_schema_and_routing_require_external_context(self):
        b=batch();b["unexpected"]="no"
        with self.assertRaises(BatchValidationError):self.adapt(b)
        b=batch();b["candidates"][0]["unexpected"]="no"
        self.assert_invalid(self.adapt(b),"schema_additionalProperties")
        service,_,_=make_service()
        with self.assertRaises(BatchValidationError):service.analyze_batch(batch(),"cfo")
        with self.assertRaises(BatchValidationError):service.analyze_batch(dict(schema_version="v2"))

    def test_empty_batch_and_legacy_format_continue_to_work(self):
        b=batch([],[]);b["status"]="empty";b["stop_reason"]="no_matches"
        self.assertFalse(self.adapt(b).analyst_input.candidates)
        service,fake,_=make_service()
        out,adapted=service.analyze_batch(make_batch(),"cfo")
        self.assertTrue(out.ranked_insights)
        self.assertIsNone(out.run_metadata.input_diagnostics)
        self.assertEqual(fake.stats.calls,1)

    def test_candidate_identity_stable_cache_depends_on_context(self):
        b=batch();r=self.adapt(b);c=r.analyst_input.candidates[0]
        def keys(c):return (cache_key("SYNTHETIC",c,"cfo","profile-1","v1","fake"),loose_key("SYNTHETIC",c,"cfo","profile-1","v1"))
        old=keys(c)
        b["request_id"]="retry"
        self.assertEqual(keys(self.adapt(b).analyst_input.candidates[0]),old)
        b["as_of"]="2026-10-25T00:00:00+03:00"
        c2=self.adapt(b).analyst_input.candidates[0]
        self.assertEqual(c.candidate_id,c2.candidate_id)
        self.assertNotEqual(keys(c2),old)
        p,i=contexts();i["interests"][0]["text"]="Другая гипотеза"
        self.assertNotEqual(keys(self.adapt(interests=i).analyst_input.candidates[0]),old)
        p["features"]["SYNTHETIC:/okved/name"]["value"]="Другое производство"
        self.assertNotEqual(keys(self.adapt(profile=p).analyst_input.candidates[0]),old)

    def test_same_event_from_multiple_reading_groups_is_one_candidate(self):
        b=batch();second=copy.deepcopy(b["candidates"][0]);second["group_ids"]=["other-group"]
        b["candidates"].append(second)
        r=self.adapt(b)
        self.assertEqual(len(r.analyst_input.candidates),1)
        self.assertEqual(r.analyst_input.candidates[0].reading_group_ids,["other-group","reading-group"])
        self.assertEqual(r.candidates[1]["state"],"duplicate")

    def test_old_raw_fallback_linked_to_deferred_group_remains_explicit(self):
        b=batch();b["deferred"]=[dict(cluster_id="reading-group",reason="merge_service_unavailable")]
        r=self.adapt(b)
        self.assertEqual(r.candidates[0]["state"],"merge_fallback")
        p=r.analyst_input.candidates[0].analysis_projection.event_bundles[0]
        self.assertEqual(p.event.processing_status,"merge_fallback")
        self.assertTrue(any("Merge недоступен" in s for s in p.limitations))

    def test_malformed_open_provenance_is_rejected_without_disclosing_text(self):
        a=source("a","СИНТЕТИКА: сообщение.")
        b=batch([merged([a])],[a]);e=b["candidates"][0]["item"]["derived_from"]["event"]
        e["fragments"][0]["article_id"]=["bad"]
        b["candidates"][0]["item"]["item_version"]=stable_hash(e)
        r=self.adapt(b)
        self.assert_invalid(r)
        self.assertNotIn(a["text"],str(r.diagnostics()))

    def test_conflicting_revisions_of_same_article_are_all_held(self):
        a=source("old","СИНТЕТИКА: первая версия.")
        b=source("new","СИНТЕТИКА: исправленная версия.")
        b["article_id"]=a["article_id"]
        r=self.adapt(batch([raw(a),raw(b)],[a,b]))
        self.assertFalse(r.analyst_input.candidates)
        self.assertEqual([c["state"] for c in r.candidates],["invalid","invalid"])

    def test_normalized_profile_hash_is_checked(self):
        p,i=contexts();p.update(schema_version="0.1",source_snapshot={"imported_at":"2026-10-23T00:00:00+00:00"})
        semantic=copy.deepcopy(p);semantic.pop("profile_version");semantic["source_snapshot"].pop("imported_at")
        p["profile_version"]=stable_hash(semantic)
        b=batch();b["profile_version"]=p["profile_version"];i["profile_version"]=p["profile_version"]
        self.assertTrue(self.adapt(b,profile=p,interests=i).analyst_input.candidates)
        p["features"]["SYNTHETIC:/okved/name"]["value"]="Изменение без обновления hash"
        with self.assertRaisesRegex(BatchValidationError,"profile_content_hash_mismatch"):
            self.adapt(b,profile=p,interests=i)

    def test_derived_numbers_are_not_whitelisted_by_qa(self):
        a=source("a","СИНТЕТИКА: тариф повышен.")
        c=merged([a]);e=c["item"]["derived_from"]["event"]
        e["title"]="Производная оценка 987654321"
        c["item"]["title"]=e["title"];c["item"]["item_version"]=stable_hash(e)
        r=self.adapt(batch([c],[a]));view=build_prompt_view(r.analyst_input,r.analyst_input.candidates[0])
        model=LLMDraft.model_validate(draft(what_happened=[dict(text="Тариф вырос на 987654321",kind="fact",claim_ids=["C1"])]))
        qa=validate_draft(model,view,"cfo",as_of=r.analyst_input.request.as_of)
        self.assertNotIn("987654321"," ".join(f.text for f in qa.facts))
        self.assertTrue(any(issue.code=="number_not_in_data" for issue in qa.issues))

    def test_fake_llm_receives_merge_only_as_derived_context(self):
        a=source("a","СИНТЕТИКА: тариф повышен.")
        c=merged([a]);summary=c["item"]["text"]
        captured=[]
        def respond(request):
            data,_=json.JSONDecoder().raw_decode(request.messages[-1]["content"].split("Данные (JSON):\n",1)[1])
            captured.append(data)
            return json.dumps(draft(),ensure_ascii=False)
        service,fake,_=make_service(responder=respond);p,i=contexts()
        service.analyze_batch(batch([c],[a]),"cfo",company_profile=p,active_interests=i)
        self.assertEqual(fake.stats.calls,1)
        event=captured[0]["event"]
        self.assertNotIn(summary,[x["text"] for x in event["claims"]])
        self.assertNotIn(summary,[x["quote"] for x in event["original_quotes"]])
        self.assertEqual(event["derived_context"][0]["text"],summary)
        self.assertFalse(event["derived_context"][0]["independent_evidence"])

    def test_final_prompt_guard_includes_actual_memory(self):
        from unittest.mock import patch
        from app.analyst.memory import MemoryItem, PreviousContext
        from app.analyst import prompts
        r=self.adapt();c=r.analyst_input.candidates[0]
        service,fake,_=make_service(extra_env={"ANALYST_PROMPT_VERSION":prompts.PROMPT_VERSION_V11})
        previous=PreviousContext(items=[MemoryItem(company_id="SYNTHETIC",role="cfo",event_id="old",event_version=1,
            published_at="2026-10-22T00:00:00+00:00",topic="other",title="Old",decision="watch",
            summary="Память "*10000,facts=())],checked=True)
        with patch.object(service.analyst,"previous_context",return_value=previous):
            out,_=service.analyst.analyze(r.analyst_input)
        self.assertEqual(out.excluded_candidates[0].reason_code,"context_too_large")
        self.assertEqual(fake.stats.calls,0)

    def test_public_examples_are_reproducible(self):
        root=Path(__file__).resolve().parents[3]/"docs/examples/retriever-analyst"
        if not root.exists():self.fail("Public examples missing")
        load=lambda name:json.loads((root/(name+".json")).read_text())
        r=adapt_retrieval_batch(load("batch"),load("company-profile"),load("active-interests"),"cfo")
        self.assertEqual(r.analyst_input.model_dump(mode="json"),load("analyst-input"))
        self.assertEqual(r.diagnostics(),load("diagnostics"))

    def assert_invalid(self, result, error=None):
        self.assertFalse(result.analyst_input.candidates)
        self.assertEqual(result.candidates[0]["state"],"invalid")
        if error:self.assertIn(error," ".join(result.candidates[0]["errors"]))


@unittest.skipUnless(os.environ.get("ANALYST_RETRIEVAL_LOCAL_DIR"), "Private local batches are opt-in")
class LocalCompatibilityTests(unittest.TestCase):
    def test_local_batches_without_copying_news(self):
        root=Path(os.environ["ANALYST_RETRIEVAL_LOCAL_DIR"])
        count=0
        for path in root.rglob("batch.json"):
            b=json.loads(path.read_text())
            folder=next((p for p in path.parents if (p/(b["company_id"]+"-profile.json")).exists()),None)
            if folder is None:continue
            profile=json.loads((folder/(b["company_id"]+"-profile.json")).read_text())
            interests=json.loads((folder/(b["company_id"]+"-interests.json")).read_text())
            r=adapt_retrieval_batch(b,profile,interests,"cfo")
            self.assertFalse([c for c in r.candidates if c["state"]=="invalid"], "Local contract incompatibility; inspect diagnostics locally")
            for c in r.analyst_input.candidates:
                for bundle in c.analysis_projection.event_bundles:
                    source_by_id={s["revision_id"]:s for s in b["sources"]}
                    for quote in bundle.evidence:
                        self.assertEqual(quote.quote,source_by_id[quote.source_id][quote.field][quote.start:quote.end])
            count+=1
        self.assertGreater(count,0)
