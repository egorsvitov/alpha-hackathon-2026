"""Synthetic contract exerciser. It does not perform financial reasoning."""
from copy import deepcopy
from llm.agent.ports import GenerationResult

SECTIONS = ('what_happened','why_important','risk','opportunity','next_step','bank_view')

class FixtureGenerator:
    mode = 'fixture'
    def __init__(self, faults=()):
        self.faults = list(faults)
        self.requests = []
    def generate(self, request):
        self.requests.append(request)
        p = request.structured_payload
        claims = []
        for c in p['claims']:
            sources = [s['source_id'] for s in p['news_sources'] if s['evidence_id'] in c['evidence_ids']]
            if not sources:
                continue
            claims.append({'claim_id': 'report:' + c['claim_id'], 'section':'what_happened', 'kind':'source_fact',
                'text':c['text'], 'source_ids': sources, 'feature_ids':[], 'assumptions':[],
                'support_status':'supported', 'input_claim_ids':[c['claim_id']],
                'modality':c['modality'], 'polarity':c['polarity']})
        draft = {'claims':claims, 'section_states':{s:{'status':'available' if s=='what_happened' and claims else 'insufficient_evidence',
                 'reason':None if s=='what_happened' and claims else 'Offline fixture does not establish this section.'} for s in SECTIONS},
                 'unknowns':[], 'limitations':['Synthetic deterministic fixture; no financial reasoning or independent semantic review.'],
                 'requested_evidence':[]}
        if p.get('conflicts'):
            draft['limitations'].append('Conflicting input variants retained; no numerical winner selected.')
        # No prose inference: unknown contractual fields remain explicit.
        for key in ('rate_type','debt_amount','repricing_date'):
            draft['unknowns'].append({'id':'contract:'+key, 'text':'Contractual '+key+' is unknown.',
                'affected_paths':['/claims'], 'clarification_question':'Уточните договорное условие: '+key+'?'})
        # Explicit synthetic rate scenarios exercise provenance and conditionality.
        # They are test templates, not model reasoning for arbitrary events.
        rate_claims = [c for c in p['claims'] if c.get('object') == 'policy_rate']
        if rate_claims and claims:
            original = rate_claims[0]
            source_refs = [s['source_id'] for s in p['news_sources'] if s['evidence_id'] in original['evidence_ids']]
            for fid, feature in sorted(p.get('features', {}).items()):
                if feature['value'] is not True or feature['quality'] in ('unknown','conflicting'):
                    continue
                product = feature['path'].rsplit('/',1)[-1]
                if product not in ('loan','deposit'):
                    continue
                assumptions = ['Неизвестны тип ставки, объём и даты пересмотра договорных условий.']
                label = 'кредита' if product == 'loan' else 'депозита'
                direction = 'стоимость обслуживания' if product == 'loan' else 'доходность'
                common = {'kind':'ai_inference', 'source_ids':source_refs, 'feature_ids':[fid],
                    'input_claim_ids':[original['claim_id']], 'assumptions':assumptions,
                    'support_status':'conditional', 'modality':None, 'polarity':None}
                claims.append({**common,'claim_id':'synthetic:'+product+':why', 'section':'why_important',
                    'text':'Если договор '+label+' допускает пересмотр ставки, '+direction+' зависит от договорных условий; сообщение источника является поводом для проверки.'})
                claims.append({**common,'claim_id':'synthetic:'+product+':next', 'section':'next_step',
                    'text':'Проверьте договор '+label+': тип ставки, объём и даты пересмотра условий пока неизвестны.'})
                for section in ('why_important','next_step'):
                    draft['section_states'][section] = {'status':'available','reason':None}
        if request.purpose == 'answer_question':
            # A fixed clarification fixture answers the supported missing-data case.
            # User-supplied facts stay unverified and never change profile values.
            for claim in claims:
                if claim['kind'] == 'ai_inference' and claim['section'] == 'next_step':
                    claim['text'] = 'Для оценки договорных последствий не хватает типа ставки, объёма кредита или депозита и даты пересмотра условий. Уточните эти сведения по договору.'
            draft['limitations'].append('Утверждения в вопросе пользователя не проверены и не изменяют сохранённый профиль.')
        fault = self.faults.pop(0) if self.faults else None
        if fault == 'foreign_source' and claims:
            claims[0]['source_ids'] = ['news:unknown']
        elif fault == 'forecast_drift' and claims:
            claims[0]['modality'] = 'asserted'; claims[0]['text'] = 'Гарантированное увеличение платежа.'
        elif fault == 'request_evidence':
            draft['requested_evidence'] = ['Нужен дополнительный банковский фрагмент по теме ставки.']
        elif fault == 'malformed':
            draft.pop('section_states')
        return GenerationResult(deepcopy(draft))
