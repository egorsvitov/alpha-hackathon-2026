"""Deterministic base3 plus anchored conditional mechanisms/catalogue hypotheses."""
from retriever.company_views import build_company_views
from retriever.representation_contracts import ViewPolicy
from retriever.experiments.activity_catalogue import query_text
from llm.event_pipeline.company_news.contracts import company_query

MECHANISMS = (
    ('/products/loan', 'credit', 'Условия кредитования бизнеса: ключевая ставка, стоимость кредита, доступность финансирования и обслуживание долга.'),
    ('/products/leasing', 'leasing', 'Условия лизинга оборудования и транспорта: аванс, ставка финансирования, налоговые изменения.'),
    ('/products/factoring', 'factoring', 'Факторинг, отсрочки оплаты и финансирование дебиторской задолженности предприятий.'),
    ('/fts_present', 'trade', 'Внешняя торговля: таможенные тарифы, валютные ограничения, международные расчёты и логистика.'),
)
VERSION = 'anchored-mechanisms.v1'


def build_queries(profile, catalogue):
    uid, pv = profile['company_id'], profile['profile_version']
    result, skipped = [], []
    for kind, view in build_company_views(profile, policy=ViewPolicy()).views.items():
        result.append(company_query(company_id=uid, profile_version=pv, text=view.text,
            family='facts', view=kind, anchor_feature_ids=sorted(r.feature_id for r in view.feature_refs),
            template_version=view.template_version, status='conditional' if view.search_topics else 'observed'))
    for path, view, text in MECHANISMS:
        f = profile['features'][uid + ':' + path]
        if f['value'] is True and f['quality'] == 'observed':
            result.append(company_query(company_id=uid, profile_version=pv, text=text,
                family='mechanisms', view=view, anchor_feature_ids=[f['feature_id']],
                template_version=VERSION, status='conditional'))
        else:
            skipped.append(dict(family='mechanisms', view=view, reason='no_observed_positive_anchor'))
    code = profile['okved']['code']
    row = catalogue.get(code)
    if row and row['status'] == 'hypothesis' and row['generator_model'] == 'gpt-6-luna':
        for kind in ('activity_topics', 'activity_finance'):
            result.append(company_query(company_id=uid, profile_version=pv, text=query_text(row, kind),
                family='hypotheses', view=kind, anchor_feature_ids=[uid + ':/okved/code', uid + ':/okved/name'],
                template_version='activity-hypotheses.v1', status='hypothesis'))
    else:
        skipped.append(dict(family='hypotheses', reason='missing_valid_catalogue_row'))
    # IDs retain provenance; inference deduplication is a separate text→vector map.
    return result, skipped
