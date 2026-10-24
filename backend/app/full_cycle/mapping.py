"""Map by candidate identity, never by array position or reading group."""
from app.analyst.bridge import to_feed_item, to_insight
from app.analyst.schemas import CandidateOutcome, CandidateRun
from app.schemas.base import Evidence, UnknownItem
from retriever.models import digest, text_hash


def map_cards(job, output, adapted, batch, profile):
    candidates = {c.candidate_id:c for c in adapted.analyst_input.candidates}
    runs = {r.candidate_id:r for r in output.run_metadata.candidates}
    raw_by_id = {('event:'+c['item']['item_id'] if c['item']['kind']=='event' else 'article:'+next(s['article_id'] for s in batch['sources'] if s['revision_id']==c['item']['source_revision_ids'][0])):c for c in batch['candidates']}
    sources = {s['revision_id']:s for s in batch['sources']}
    cards = []
    for insight in output.ranked_insights:
        cand = candidates[insight.candidate_id]
        # Adapter event refs carry the upstream item ID unchanged (canonical ID
        # is separately prefixed by kind). This survives rejection and dedup.
        item_id = cand.event_bundle_refs[0].event_id
        raw = raw_by_id[item_id]
        item = raw['item']
        originals = [sources[r] for r in item['source_revision_ids']]
        timestamps = [s['published_at'] for s in originals]
        known = all(s['publication_precision'] == 'timestamp' for s in originals)
        if known:
            from retriever.models import instant
            published = min(timestamps, key=instant)
            precision = 'timestamp'
        elif len(originals) == 1:
            published, precision = originals[0]['published_at'], originals[0]['publication_precision']
        else:
            published, precision = None, 'unknown'
        context = digest(dict(profile=profile, interests=job['interest_snapshot'], role=job['role'],
                              as_of=job['as_of'], policy=job['analysis_policy'], analyst=job['analyst_signature'],
                              event_version=item['item_version'], event_text_sha256=text_hash(item['text'])))
        card_id = digest([job['company_id'],job['role'],cand.canonical_event_id,context])
        limitations = list(dict.fromkeys(batch['limitations'] + raw.get('limitations', []) +
            ['Идентичность события между повторными разбиениями не гарантирована; версии не объединяются автоматически.']))
        event = dict(event_id=card_id, title=item['title'], date=published, publication_precision=precision,
            topic='other', topic_label='Новости', sources_count=len(originals), is_synthetic=profile.get('is_synthetic',False),
            canonical_event_id=cand.canonical_event_id, candidate_id=cand.candidate_id,
            reading_group_ids=cand.reading_group_ids, revision_ids=item['source_revision_ids'],
            text_version=item['item_version'], text_sha256=text_hash(item['text']),
            derived_text=item['text'] if item['kind']=='event' else None, derived_text_origin='derived_merge' if item['kind']=='event' else None,
            processing_status='merge_fallback' if raw.get('limitations') and any('fallback' in s.lower() for s in raw['limitations']) else 'ready',
            limitations=limitations, sources=[dict(revision_id=s['revision_id'], article_id=s['article_id'],
                title=s['title'], publisher=s['source'], url=s.get('url'), published_at=s['published_at'],
                publication_precision=s['publication_precision'], text_sha256=s['text_sha256']) for s in originals])
        outcome = CandidateOutcome(insight=insight, run=runs.get(cand.candidate_id) or CandidateRun(
            candidate_id=cand.candidate_id,prompt_version=output.run_metadata.prompt_version))
        feed = to_feed_item(outcome, event, output.run_metadata.profile_version)
        card = to_insight(outcome, event=event, cand=cand, ctx=adapted.analyst_input.company_context,
                          company_id=job['company_id'], role=job['role'],run_id=job['id'])
        # The legacy bridge intentionally exposes only referenced evidence. For
        # full-cycle cards append every checked original fragment with coordinates.
        checked = []
        for bundle in cand.analysis_projection.event_bundles:
            for evidence in bundle.evidence:
                s = sources[evidence.source_id]
                payload = evidence.model_dump()
                checked.append(Evidence(evidence_id=evidence.evidence_id, type='synthetic' if profile.get('is_synthetic') else 'news',
                    title=s['title'], publisher=s['source'], date=s['published_at'], url=s.get('url'), quote=evidence.quote,
                    is_synthetic=profile.get('is_synthetic',False), revision_id=s['revision_id'],
                    field=payload.get('field'), start=payload.get('start'), end=payload.get('end'),
                    text_sha256=s['text_sha256'], publication_precision=s['publication_precision']))
        original_ids = {e.evidence_id for e in checked}
        card.evidence = checked + [e for e in card.evidence if e.evidence_id not in original_ids]
        for evidence in card.evidence:
            if evidence.type == 'company_feature':
                evidence.is_synthetic = profile.get('is_synthetic', False)
                evidence.publisher = 'Профиль компании'
        card.unknowns.extend(UnknownItem(text=s) for s in limitations)
        cards.append(dict(id=card_id, canonical_event_id=cand.canonical_event_id, context_hash=context,
                          candidate_id=cand.candidate_id, decision=insight.decision, feed=feed.model_dump(mode='json'),
                          insight=card.model_dump(mode='json')))
    return cards
