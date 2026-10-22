"""Conservative offline structural checks and narrow critical publication guards.

These guards are not a semantic reviewer. In particular bank fragment metadata,
word overlap and a generator's own confidence never establish a bank opinion.
"""
from pathlib import Path
import json
import re
from jsonschema import Draft202012Validator, FormatChecker
from .ports import QAReport

_NUMBERS = re.compile(r'(?<!\w)[+-]?\d+(?:[.,]\d+)?(?:\s*%|\s*п\.?\s*п\.?)?', re.I)


def _tokens(text):
    return {re.sub(r'\s+', '', x).replace(',', '.').casefold() for x in _NUMBERS.findall(text)}


def validate_draft(draft, context, registry):
    issues = []
    checks = ['draft_schema', 'identity_and_refs', 'source_exact_text', 'registry_integrity',
              'linked_number_guard', 'unknown_exposure_guard', 'bank_view_guard']
    def issue(code, path, message, claim_id=None, repairable=True):
        issues.append(dict(code=code, severity='error', claim_id=claim_id, path=path,
                           message=message, repairable=repairable))
    schema_path = Path(__file__).parent / 'prompts' / 'draft.schema.json'
    if not schema_path.exists():
        raise RuntimeError('draft_schema_unavailable')
    schema = json.loads(schema_path.read_text())
    for err in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(draft):
        issue('draft_schema', '/' + '/'.join(str(x) for x in err.absolute_path), err.message)
    if issues:
        return QAReport(status='failed',next_action='repair_draft',issues=issues,checks_run=checks,
                        structural_status='failed',publication_status='block')
    profile = context.profile
    snap = context.snapshot
    bundle = snap['bundle']
    expected = {'company_id':profile['company_id'],'profile_version':profile['profile_version'],
                'event_id':snap['event_id'],'event_version':snap['event_version'], 'role':context.role}
    for key, value in expected.items():
        if key in draft and draft[key] != value:
            issue('identity_mismatch','/'+key,'Draft refers to another pinned identity', repairable=False)
    if context.registry != registry:
        issue('registry_mutation','/sources','Registry differs from pinned context', repairable=False)
    for field in ('sources', 'source_registry', 'registry'):
        if field in draft:
            issue('registry_override','/'+field,'Generator cannot supply or overwrite registry')
    evidence = {e['evidence_id']:e for e in bundle['evidence']}
    for sid, source in registry.items():
        if source.get('type') != 'bank_analytics':
            original = evidence.get(source.get('evidence_id'))
            if (original is None or source.get('quote') != original['quote'] or
                source.get('event_id') != snap['event_id'] or source.get('event_version') != snap['event_version']):
                issue('registry_provenance_mismatch','/sources/'+sid,'News source differs from pinned B6 evidence',repairable=False)
    for key in ('company_id','profile_version','event_id','event_version'):
        if context.relevance.get(key) != expected[key]:
            issue('context_identity_mismatch','/context/relevance/'+key,'Pinned relevance identity mismatch',repairable=False)
    if context.relevance.get('user_role') != context.role:
        issue('context_identity_mismatch','/context/relevance/user_role','Pinned role mismatch',repairable=False)
    inputs = {c['claim_id']:c for c in bundle['claims']}
    features = profile['features']
    for index, claim in enumerate(draft['claims']):
        path = '/claims/'+str(index)
        if not isinstance(claim, dict):
            continue
        cid = claim.get('claim_id')
        text = claim.get('text','')
        if not isinstance(text, str):
            continue
        refs = claim.get('source_ids',[])
        fids = claim.get('feature_ids',[])
        input_ids = claim.get('input_claim_ids',[])
        if not all(isinstance(x,list) for x in (refs,fids,input_ids)):
            continue
        linked = [registry[r] for r in refs if isinstance(r,str) and r in registry]
        if any(r in context.excluded_artifacts for r in refs):
            issue('excluded_source',path+'/source_ids','Source was excluded from bounded generation context',cid)
        if not refs or len(linked) != len(refs):
            issue('unknown_source',path+'/source_ids','All claims require existing sources',cid)
        if any(not isinstance(f,str) or f not in features for f in fids):
            issue('unknown_feature',path+'/feature_ids','Feature belongs to another or missing profile',cid)
        if any(not isinstance(i,str) or i not in inputs for i in input_ids):
            issue('unknown_input_claim',path+'/input_claim_ids','Input claim must belong to current event',cid)
        source_claims = [inputs[i] for i in input_ids if isinstance(i,str) and i in inputs]
        # A reference must actually support the named B6 claim, not just exist.
        for original in source_claims:
            if not any(s.get('evidence_id') in original['evidence_ids'] or
                       bool(set(s.get('evidence_ids',[])) & set(original['evidence_ids'])) for s in linked):
                issue('unlinked_source',path+'/source_ids','Source does not evidence the named input claim',cid)
        if claim.get('kind') == 'source_fact':
            if len(source_claims) != 1 or text != source_claims[0]['text']:
                issue('source_text_drift',path+'/text','Source fact must exactly preserve one B6 claim',cid)
            for original in source_claims:
                for key in ('modality','polarity'):
                    if claim.get(key) != original[key]:
                        issue('source_'+key+'_drift',path+'/'+key,'Source modality or polarity changed',cid)
        if 'quote' in claim:
            if not any(claim['quote'] == s.get('quote') for s in linked):
                issue('quote_override',path+'/quote','Quote is not an immutable registry quote',cid)
        bank = claim.get('kind') == 'bank_view' or claim.get('section') == 'bank_view'
        if not bank and re.search(r'(?:альфа[ -]?банк|банк)\s+(?:считает|ожидает|рекомендует|полагает|прогнозирует)', text, re.I):
            issue('bank_attribution_unreviewed',path+'/text','Bank attribution requires verified bank opinion',cid)
        if bank:
            issue('bank_meaning_unreviewed',path,'Bank opinion requires independent semantic verification',cid)
            if not any(s.get('type') == 'bank_analytics' for s in linked):
                issue('bank_source_required',path+'/source_ids','Bank opinion cannot be grounded in news',cid)
            if any(s.get('visually_checked') is True and not s.get('visual_review_provenance') for s in linked):
                issue('unverified_visual_review',path,'Visual review requires explicit provenance',cid)
        if claim.get('kind') == 'ai_inference':
            if any(original.get('scenario') is not None for original in source_claims):
                if claim.get('support_status') != 'conditional' or not claim.get('assumptions'):
                    issue('scenario_inference_unconditional',path,'Scenario-based inference must retain explicit conditions',cid)
            if _tokens(text) and any(m.get('value_role') in ('before','after','comparison')
                                    for original in source_claims for m in original.get('measurements',[])):
                issue('measurement_role_inference_unreviewed',path,'Numerical inference from before/after/comparison requires semantic review; report source facts instead',cid)
            conflicted = {v['claim_id'] for conflict in bundle.get('conflicts',[]) for v in conflict['variants']}
            conflict_numbers = set()
            for original in inputs.values():
                if original['claim_id'] in conflicted:
                    conflict_numbers.update(_tokens(original['text']))
            if _tokens(text) & conflict_numbers:
                issue('conflicting_number_inference',path+'/text','Inference cannot select an unresolved numeric conflict',cid)
            if not fids:
                issue('feature_required',path+'/feature_ids','Personal inference requires profile features',cid)
            selected = [features[f] for f in fids if isinstance(f,str) and f in features]
            unknown = any(f.get('quality') in ('unknown','conflicting') or f.get('value') is None for f in selected)
            # Products alone never supply contractual exposure (rate/amount/repricing).
            exposure = any(re.search(r'loan|leasing|deposit|кредит|лизинг|вклад|ставк|платеж|платёж|финанс|расход|доход|прибыл|выручк|денеж|эконом|убыт|затрат|выплат|стоим',text,re.I) for _ in [0])
            if unknown or exposure:
                if claim.get('support_status') != 'conditional' or not claim.get('assumptions'):
                    issue('unknown_exposure_unconditional',path,'Unknown contractual exposure requires explicit assumptions and conditional support',cid)
            if any(f.get('quality') == 'conflicting' for f in selected) and claim.get('support_status') != 'conditional':
                issue('conflicting_feature',path,'Conflicting profile value cannot be treated as agreed',cid)
        allowed = set()
        for s in linked:
            allowed.update(_tokens(s.get('quote','')))
        for original in source_claims:
            allowed.update(_tokens(original['text']))
        for fid in fids:
            if isinstance(fid,str) and fid in features and features[fid].get('quality') not in ('unknown','conflicting'):
                allowed.update(_tokens(str(features[fid].get('value',''))))
        if _tokens(text) - allowed:
            issue('unsupported_number',path+'/text','Number is absent from this claim’s linked evidence/features',cid)
    for section, state in draft.get('section_states',{}).items():
        if not isinstance(state,dict):
            continue
        present = any(isinstance(c,dict) and c.get('section') == section for c in draft['claims'])
        if (state.get('status') == 'available') != present:
            issue('section_state_mismatch','/section_states/'+section,'Available section requires claims; empty sections need explicit evidence state')
        if not present and not state.get('reason'):
            issue('section_reason_required','/section_states/'+section,'Empty section requires an explanation')
    failed = bool(issues)
    requested = draft.get('requested_evidence',[])
    return QAReport(status='failed' if failed else 'limited', next_action='repair_draft' if failed else ('retrieve_more' if requested else 'deliver'),
                    missing_evidence_queries=list(requested), issues=issues, checks_run=checks,
                    checks_not_run=['independent_semantic_review','general_financial_reasoning','bank_opinion_semantics'],
                    structural_status='failed' if failed else 'passed',semantic_status='not_run',
                    publication_status='block' if failed else 'limited')


class StructuralQA:
    def check(self, draft, context, registry):
        return validate_draft(draft, context, registry)
