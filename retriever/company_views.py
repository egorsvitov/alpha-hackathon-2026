"""Deterministic supported company facts and conditional search topics."""
from __future__ import annotations
import json
from pathlib import Path
from ranking.contracts import resolve_pointer
from .representation_contracts import (
    CompanyView, CompanyViews, FeatureRef, InvalidProfileReference, ViewPolicy,
    content_hash, normalize_embedding_text, text_hash,
)

DEFAULT_TEMPLATES = Path(__file__).parent / 'config/company-view-templates.v1.json'


def _templates(policy):
    if policy.normalization != 'nfc-whitespace.v1':
        raise ValueError('unsupported text normalization')
    config = json.loads(Path(policy.template_config_path or DEFAULT_TEMPLATES).read_text(encoding='utf-8'))
    digest = content_hash(config)
    if policy.template_config_hash and policy.template_config_hash != digest:
        raise ValueError('template config hash mismatch')
    return config, policy.version + ':' + config['version'] + ':' + digest


def build_company_views(profile: dict, *, policy: ViewPolicy) -> CompanyViews:
    config, version = _templates(policy)
    uid, pv = profile['company_id'], profile['profile_version']
    views, omitted = {}, {}

    def field(path):
        fid = uid + ':' + path
        feature = profile.get('features', {}).get(fid)
        try:
            value = resolve_pointer(profile, path)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise InvalidProfileReference(fid) from exc
        if not feature or feature.get('feature_id') != fid or feature.get('path') != path or feature.get('value') != value:
            raise InvalidProfileReference(fid)
        return feature

    def make(kind, facts, topics, selected, limitations=()):
        # Semantic selection never contains IDs, source hashes, records or timestamps.
        unknowns = sorted(p for p, f in selected.items() if f['value'] is None or f.get('quality') in {'unknown', 'conflicting'})
        refs = [FeatureRef(uid, pv, uid + ':' + p) for p in sorted(selected)]
        def evidence(text, paths, topic=False):
            key = 'basis_feature_refs' if topic else 'feature_refs'
            result = {'text': normalize_embedding_text(text), key: [FeatureRef(uid, pv, uid + ':' + p).to_dict() for p in paths]}
            if topic:
                result['status'] = 'conditional'
            return result
        fact_items = [evidence(t, ps) for t, ps in facts]
        topic_items = [evidence(t, ps, True) for t, ps in topics]
        text = normalize_embedding_text(' '.join(t for t, _ in facts))
        if topics:
            text += ' Темы для проверки: ' + '; '.join(t for t, _ in topics) + '.'
        limitations = sorted(set(limitations))
        semantic = {'kind': kind, 'template_version': version, 'selected': {p: {'value': f['value'], 'quality': f['quality']} for p, f in selected.items()}, 'unknowns': unknowns, 'limitations': limitations}
        return CompanyView(uid, pv, kind, text, text_hash(text), content_hash(semantic), version,
            refs, fact_items, topic_items, 'limited' if unknowns or limitations else 'observed', unknowns,
            limitations, {'profile_version': pv, 'source_snapshot': profile.get('source_snapshot', {}),
                          'features': {p: f.get('origin') for p, f in selected.items()}})

    selected = {p: field(p) for p in ('/okved/name', '/okved/code', '/industry')}
    name, code, industry = (selected[p]['value'] if selected[p]['quality'] in {'observed', 'derived'} else None for p in selected)
    label = config['industry_labels'].get(industry)
    facts = []
    if name:
        facts.append((f'Основная деятельность: {name}.', ['/okved/name']))
    if name or label:
        if code:
            facts.append((f'ОКВЭД: {code}.', ['/okved/code']))
        if label:
            facts.append((f'Отрасль: {label}.', ['/industry']))
        views['business'] = make('business', facts, [], selected)
    else:
        omitted['business'] = 'no_supported_activity'

    selected = {f'/products/{key}': field(f'/products/{key}') for key, _, _ in config['products']}
    positive = [(key, label, topic) for key, label, topic in config['products'] if selected['/products/' + key]['value'] is True and selected['/products/' + key]['quality'] == 'observed']
    if positive:
        paths = ['/products/' + key for key, _, _ in positive]
        facts = [('В записи банка отмечены продукты: ' + ', '.join(label for _, label, _ in positive) + '.', paths)]
        views['finance'] = make('finance', facts, [(topic, ['/products/' + key]) for key, _, topic in positive], selected,
                                ['products_describe_supplied_bank_record_only'])
    else:
        omitted['finance'] = 'no_positive_products'

    selected = {p: field(p) for p in ('/region', '/fts_present', '/countries')}
    region, fts, countries = (selected[p] for p in selected)
    facts, limitations = [], []
    if region['value'] and region['quality'] == 'observed':
        facts.append((f"Регион регистрации: {region['value']}.", ['/region']))
        limitations.append('registered_region_not_all_operations')
    if fts['value'] is True and fts['quality'] == 'observed':
        facts.append(('В записи отмечено участие во внешней торговле.', ['/fts_present']))
    if countries['value'] and countries['quality'] == 'observed' and fts['value'] is not False:
        names = [config['country_labels'].get(c) for c in sorted(countries['value'])]
        if all(names):
            if fts['value'] is True and fts['quality'] == 'observed':
                facts.append(('Указанные страны ВЭД: ' + ', '.join(names) + '.', ['/countries', '/fts_present']))
            else:
                facts.append(('В записи указаны страны ' + ', '.join(names) + '; участие требует уточнения.', ['/countries', '/fts_present']))
                limitations.append('trade_participation_requires_confirmation')
    if facts:
        views['trade_geo'] = make('trade_geo', facts, [], selected, limitations)
    else:
        omitted['trade_geo'] = 'no_supported_trade_or_registered_region'
    return CompanyViews(uid, pv, views, omitted)


def build_universal_view(views: CompanyViews, *, policy: ViewPolicy) -> CompanyView | None:
    _, version = _templates(policy)
    items = [views.views[k] for k in ('business', 'finance', 'trade_geo') if k in views.views]
    if not items:
        return None
    text = normalize_embedding_text(' '.join(v.text for v in items))
    refs = sorted({r for v in items for r in v.feature_refs}, key=lambda r: r.feature_id)
    return CompanyView(views.company_id, views.profile_version, 'universal', text, text_hash(text),
        content_hash({'kind': 'universal', 'template_version': version, 'base_content_hashes': [v.content_hash for v in items]}), version,
        refs, [f for v in items for f in v.facts], [t for v in items for t in v.search_topics],
        'limited' if any(v.quality == 'limited' for v in items) else 'observed',
        sorted({u for v in items for u in v.unknowns}), sorted({u for v in items for u in v.limitations}),
        {'base_views': {v.kind: v.source_provenance for v in items}})
