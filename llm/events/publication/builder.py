"""B5 → EventBundle v0.2, with a compatibility wrapper for existing callers."""
from datetime import datetime
from llm.events.contracts.models import Event, EventBundle
from llm.events.publication.features import derive_features, assess_quality, resolved_rules, _title, build_bundle as build_public_bundle, VERIFICATION_LIMIT
from llm.events.publication.models import PublicationConfig, PublicationContext

RULES_VERSION = 'b6.1'
# Kept as a public compatibility constant. Rules themselves are versioned data.
TYPE_LABELS = dict(resolved_rules(PublicationConfig()).category_titles)


def build_bundle(record, context=None, config=None, *, public_version=1,
                 published_at=None, contexts=(), b4_qualities=None, decisions=(),
                 claim_events=None, missing_articles=(), updated_at=None):
    """Return the public bundle. New callers supply a PublicationContext.

    Legacy projection callers may still supply B5 contexts and B4 quality flags;
    publication always uses the full, archived context and checks provenance.
    """
    config = config or PublicationConfig()
    stamp = published_at or updated_at
    if context is not None:
        return build_public_bundle(record, context, config,
            public_version=public_version, published_at=stamp).bundle
    rules = resolved_rules(config).model_copy(deep=True)
    rules.category_titles.update(TYPE_LABELS)
    context = PublicationContext(articles=[], decisions=list(decisions),
        missing_articles=list(missing_articles), catalog_claim_ids=sorted(claim_events or {}))
    quality, reasons = assess_quality(record, context)
    ids = {c.claim_id for c in record.claims}
    incomplete = any(c.claim.claim_id in ids and
        (b4_qualities or {}).get(c.artifact_id) != 'complete' for c in contexts)
    if incomplete and quality == 'complete':
        quality = 'partial'
    features, _ = derive_features(record, rules)
    limits = set(record.limitations) | {VERIFICATION_LIMIT} | {r.message for r in reasons}
    if incomplete:
        limits.add('B4 processing is incomplete')
    if record.replaced_by:
        limits.add('Replaced by event IDs: ' + ', '.join(sorted(record.replaced_by)))
    dates = [s.published_at for s in record.sources if s.published_at]
    return EventBundle(schema_version='0.2',
        event=Event(event_id=record.event_id, event_version=public_version,
            title=_title(record, rules), event_type=record.event_type,
            first_published_at=min(dates, key=datetime.fromisoformat) if dates else None,
            updated_at=stamp, availability=record.availability, quality=quality),
        ranking_features=features, claims=record.claims, sources=record.sources,
        evidence=record.evidence, conflicts=record.conflicts, relations=record.relations,
        limitations=sorted(limits))
