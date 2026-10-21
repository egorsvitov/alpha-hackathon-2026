"""Deterministic B5 -> EventBundle v0.2 projection; no IO or inference."""
from datetime import datetime

from .matching import describe_identity
from .models import Event, EventBundle, RankingFeatures

RULES_VERSION = 'b6.1'
# These are retrieval hints, never estimates of company exposure.
TYPE_LABELS = {
    'policy_rate': 'Ключевая ставка', 'fx_rate': 'Валютный курс',
    'price_change': 'Изменение цены', 'regulation': 'Регулирование',
    'sanction_or_trade_restriction': 'Санкции и торговые ограничения',
    'market_indicator': 'Рыночный показатель', 'company_results': 'Результаты компании',
    'investment': 'Инвестиции', 'corporate_action': 'Корпоративное действие',
    'operational_incident': 'Операционный инцидент', 'other': 'Событие',
}
TYPE_HINTS = {
    'policy_rate': ('rates', 'interest_rates'), 'fx_rate': ('fx', 'fx'),
    'regulation': ('other', 'regulation'),
    'sanction_or_trade_restriction': ('sanctions', 'sanctions'),
    'company_results': ('counterparty', None), 'investment': ('industry', 'investments'),
    'corporate_action': ('counterparty', None), 'operational_incident': ('industry', None),
}
LABEL_HINTS = {
    'policy_rate': ('rates', 'interest_rates'),
    'official_exchange_rate': ('fx', 'fx'), 'market_exchange_rate': ('fx', 'fx'),
    'inflation': ('inflation', 'inflation'), 'consumer_price_index': ('inflation', 'inflation'),
    'steel_consumption': ('industry', 'demand'),
    'import_tariff': ('trade', 'tariffs'), 'export_tariff': ('trade', 'tariffs'),
}
OBJECT_INDUSTRIES = {
    'steel': 'steel', 'steel_consumption': 'steel', 'oil': 'oil_gas_chemicals',
    'natural_gas': 'oil_gas_chemicals', 'electricity': 'energy',
}


def build_bundle(record, *, contexts, b4_qualities, decisions=(),
                 claim_events=None, missing_articles=(), updated_at):
    """Project a record and structured metadata from the same catalog run.

    event_version=1 is provisional; publication assigns the B6 sequence.
    Callers validate structure and archived grounding before publication.
    """
    claim_events = claim_events or {}
    claims = record.claims
    ids = {c.claim_id for c in claims}
    objects = sorted({c.object for c in claims if c.object})
    metrics = sorted({m.metric for c in claims for m in c.measurements})
    topics, factors = set(), set()
    if claims:
        hints = [TYPE_HINTS.get(record.event_type)]
        hints += [LABEL_HINTS.get(label) for label in objects + metrics]
        for hint in hints:
            if hint:
                topics.add(hint[0])
                if hint[1]:
                    factors.add(hint[1])
    limitations = set(record.limitations)
    incomplete = set()
    for ctx in contexts:
        if ctx.claim.claim_id not in ids:
            continue
        if b4_qualities.get(ctx.artifact_id) != 'complete':
            incomplete.add('B4 processing is incomplete for claim: ' + ctx.claim.claim_id)
        missing = describe_identity(ctx).missing
        if missing:
            incomplete.add('Missing identity features for ' + ctx.claim.claim_id + ': ' + ', '.join(missing))
    for claim in claims:
        unknown = [name for name in ('action', 'object', 'modality', 'polarity')
                   if getattr(claim, name) in (None, 'unknown')]
        if unknown:
            incomplete.add('Unknown fields for ' + claim.claim_id + ': ' + ', '.join(unknown))
    if claims and missing_articles:
        incomplete.add('Missing current B4 inputs for: ' + ', '.join(sorted(missing_articles)))
    for decision in decisions:
        if not ids.intersection((decision.left_id, decision.right_id)):
            continue
        if decision.left_id not in claim_events or decision.right_id not in claim_events:
            continue  # An archived updates target does not create uncertainty.
        if decision.identity == 'uncertain':
            incomplete.add('Some candidate event identities remain uncertain')
        if (decision.identity == 'same_event' and
                claim_events[decision.left_id] != claim_events[decision.right_id]):
            incomplete.add('Some same-event pairs could not join a fully compatible group')
    if record.availability == 'withdrawn' or not claims:
        quality = 'insufficient'
        limitations.add('No current factual basis for analysis; withdrawal is not a refutation')
    else:
        quality = 'partial' if incomplete else 'complete'
        limitations.update(incomplete)
    if record.replaced_by:
        limitations.add('Replaced by event IDs: ' + ', '.join(sorted(record.replaced_by)))
    dates = [s.published_at for s in record.sources if s.published_at is not None]
    first = min(dates, key=datetime.fromisoformat) if dates else None
    title = TYPE_LABELS[record.event_type]
    if objects:
        title += ': ' + ', '.join(objects)
    return EventBundle(
        schema_version='0.2',
        event=Event(event_id=record.event_id, event_version=1, title=title,
                    event_type=record.event_type, first_published_at=first,
                    updated_at=updated_at, availability=record.availability, quality=quality),
        ranking_features=RankingFeatures(topics=sorted(topics or {'other'}),
            industries=sorted({OBJECT_INDUSTRIES[o] for o in objects if o in OBJECT_INDUSTRIES}),
            geographies=sorted({g for c in claims for g in c.geographies}),
            objects=objects, metrics=metrics, factors=sorted(factors)),
        claims=record.claims, sources=record.sources, evidence=record.evidence,
        conflicts=record.conflicts, relations=record.relations, limitations=sorted(limitations))
