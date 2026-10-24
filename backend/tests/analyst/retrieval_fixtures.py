"""Entirely synthetic wire fixtures. No local news or private profiles."""
from app.analyst.input_adapter import stable_hash
from app.analyst.retrieval_adapter import text_hash


def source(rid, text, *, published_at="2026-10-23T09:00:00+00:00", precision="timestamp"):
    return dict(revision_id=rid, article_id="synthetic:" + rid, text=text, text_sha256=text_hash(text),
                title="Синтетическая публикация " + rid, title_origin="source", source="SYNTHETIC Publisher",
                published_at=published_at, publication_precision=precision, observed_at=None,
                url="https://example.org/synthetic/" + rid, language="ru", provenance=[])


def raw(s, group="reading-group"):
    return dict(item=dict(item_id=s["revision_id"], item_version="1", kind="article", title=s["title"], text=s["text"],
                          embedding_text=s["text"], source_revision_ids=[s["revision_id"]], derived_from={},
                          was_merged=False, publication_count=1, source_count=0),
                selection_score=0.8, embedding_score=0.8, score_origin="cosine", best_interest_id="energy",
                interest_scores={"energy": 0.8}, group_ids=[group], limitations=[])


def merged(sources, eid="event-a", *, group="reading-group", slices=None):
    fs = []
    for s in sources:
        start, end = (slices or {}).get(s["revision_id"], (0, len(s["text"])))
        text = s["text"][start:end]
        fs.append(dict(id=stable_hash([s["revision_id"], "text", start, end, text]), article_id=s["revision_id"],
                       field="text", start=start, end=end, text=text))
    summary = "Производный синтетический пересказ: опубликовано изменение условий."
    event = dict(id=eid, title="Синтетическое объединённое событие", text=summary,
                 embedding_text="Синтетическое объединённое событие\n\n" + summary,
                 article_ids=sorted(s["revision_id"] for s in sources), fragments=fs,
                 paragraphs=[dict(text=summary, fragment_ids=[f["id"] for f in fs])], disagreements=[],
                 was_merged=len(sources)>1, publication_count=len(sources), source_count=1)
    c = raw(sources[0], group)
    c["item"] = dict(item_id=eid, item_version=stable_hash(event), kind="event", title=event["title"], text=event["text"],
                     embedding_text=event["embedding_text"], source_revision_ids=event["article_ids"], was_merged=event["was_merged"],
                     publication_count=len(sources), source_count=1,
                     derived_from=dict(event=event, cluster_id=group, cluster_revision="1",
                                       operation_id="a"*64, result_id="b"*64, configuration_fingerprint="c"*64))
    c["group_ids"] = []  # Real merged items retain group identity in derived_from.
    return c


def contexts():
    fid = "SYNTHETIC:/okved/name"
    profile = dict(company_id="SYNTHETIC", profile_version="profile-1", is_synthetic=True,
                   features={fid: dict(feature_id=fid, path="/okved/name", value="Синтетическое производство",
                                       quality="observed", origin=dict(kind="synthetic", source_path="public-example"))},
                   missing_fields=[], limitations=["Полностью синтетический публичный пример."])
    interests = dict(company_id="SYNTHETIC", profile_version="profile-1", interest_set_version="interests-1",
                     encoder_fingerprint="synthetic-encoder", dimension=2,
                     interests=[dict(interest_id="energy", text="Возможное влияние тарифов; зависимость компании неизвестна",
                                     family="activity", active=True, vector=[1.0,0.0],
                                     applicable_when="Требует проверки энергозатрат", applicability_basis="Гипотеза по виду деятельности")])
    return profile, interests


def batch(candidates=None, sources=None):
    s = source("article-a", "СИНТЕТИКА: опубликован проект изменения тарифа на электроэнергию.")
    return dict(schema_version="retrieval-candidate-batch.v1", request_id="synthetic-request", company_id="SYNTHETIC",
                profile_version="profile-1", interest_set_version="interests-1", corpus_id="synthetic-corpus",
                index_generation=1, encoder_fingerprint="synthetic-encoder", as_of="2026-10-24T00:00:00+03:00",
                requested_k=20, status="ready", stop_reason="candidate_limit", trace_id="synthetic-trace",
                candidates=[raw(s)] if candidates is None else candidates, sources=[s] if sources is None else sources,
                deferred=[], limitations=["Сходство при отборе не является бизнес-значимостью."])
