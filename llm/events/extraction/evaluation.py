"""Explicit human alignment; no fabricated negatives or automatic semantic ground truth."""
from collections import Counter
from pathlib import Path
import json
from llm.events.normalization.core import validate_normalized_result
from llm.events.normalization.models import NormalizedArticleResult
from llm.events.storage.store import StorageError
from llm.events.contracts.validation import pointer, article_sha256
from llm.events.extraction.protocol import digest
from llm.events.extraction.service import owner_for
import llm.events.extraction.prompts as prompts


def contains(actual,expected):
    if isinstance(expected,dict):return isinstance(actual,dict) and all(k in actual and contains(actual[k],v) for k,v in expected.items())
    if isinstance(expected,list):
        if not isinstance(actual,list):return False
        def assign(i,used):
            if i==len(expected):return True
            return any(contains(a,expected[i]) and assign(i+1,used|{j}) for j,a in enumerate(actual) if j not in used)
        return assign(0,set())
    return type(actual)==type(expected) and actual==expected


def compare_variants(store,a_artifact_ids,b_artifact_ids,*,gold=None,alignments=None,calls=None):
    selected={};variants={};verification_decisions={}
    for variant,ids in [('A',a_artifact_ids),('B',b_artifact_ids)]:
        results=[];claims={}
        for aid in ids:
            artifact=store.get_artifact(aid);owner=owner_for(store,artifact)
            if owner.spec.processor_name!='events.normalize_claims' or artifact.kind not in ('normalized_claims','normalization_result'):
                raise StorageError('invalid_comparison_input','Expected B4 result')
            if artifact.kind=='normalized_claims' and (owner.state!='succeeded' or owner.result_artifact_id!=aid):
                raise StorageError('invalid_comparison_input','Expected successful final normalization')
            if artifact.kind=='normalization_result' and owner.state!='failed':
                raise StorageError('invalid_comparison_input','Expected failed diagnostic normalization')
            result=NormalizedArticleResult.model_validate(artifact.payload);record=store.get_article(owner.source_id)
            validate_normalized_result(result,record)
            upstream=owner_for(store,store.get_artifact(result.input_artifact_id))
            if upstream.spec.configuration.get('variant')!=variant:raise StorageError('variant_mismatch','Comparison input variant differs')
            article_id=record.article.article_id
            if variant=='B':
                if upstream.spec.configuration.get('normalized_a_artifact_id') not in a_artifact_ids:
                    raise StorageError('input_provenance_mismatch','B does not derive from selected A')
                for review_id in upstream.spec.configuration.get('verification_artifact_ids',[]):
                    review=store.get_artifact(review_id).payload
                    for selection in review['selections']:
                        verification_decisions[(article_id,selection['candidate_id'])]=selection['accepted']
            if article_id in claims:raise StorageError('duplicate_article_result','Select one result per article/variant')
            claims[article_id]={c.candidate_id:c for c in result.accepted};selected[article_id]=record
            results.append({'article_id':article_id,'artifact_id':aid,'candidates':result.candidate_count,
                'accepted':len(result.accepted),'rejected':len(result.rejected),'quality':result.quality,
                'all_excluded':variant=='B' and not result.accepted,'diagnostics':[d.model_dump(mode='json') for d in result.diagnostics]})
        variants[variant]={'articles':results,'claims':claims}
    if gold is not None:
        if gold.get('schema_version')!='extraction-gold.v1' or gold.get('status')!='single_annotator_draft':
            raise StorageError('invalid_gold','Expected frozen manual development draft')
        hashes={r['article_id']:r['article_sha256'] for r in gold['articles']}
        for aid,record in selected.items():
            if hashes.get(aid)!=article_sha256(record.article):raise StorageError('gold_source_mismatch','Gold belongs to another article text')
        for c in gold['cases']:
            record=selected.get(c['article_id'])
            if record:
                e=c['evidence']
                if getattr(record.article,e['field'])[e['start']:e['end']]!=e['quote']:
                    raise StorageError('gold_quote_mismatch','Gold evidence does not match archive')
    mappings=alignments or []
    if mappings and gold is None:raise StorageError('missing_gold','Alignments require a gold set')
    cases={c['gold_id']:c for c in (gold or {}).get('cases',[])}
    seen=set();field_results=[];review_counts=Counter();matched=set();human_judgments={};reviewed_missing=set();recalled_a=set();recalled_b=set()
    for item in mappings:
        key=(item['article_id'],item.get('candidate_id'),item.get('gold_id'))
        if key in seen:raise StorageError('duplicate_alignment','Duplicate human alignment')
        seen.add(key)
        if item.get('candidate_id') is None:
            case=cases.get(item.get('gold_id'))
            if case is None or case['article_id']!=item['article_id'] or item.get('missed') is not True:
                raise StorageError('invalid_alignment','An explicit missing gold case requires missed=true')
            reviewed_missing.add(item['gold_id']);continue
        if item.get('gold_id') is None:
            a=variants['A']['claims'].get(item['article_id'],{}).get(item['candidate_id'])
            judgment=item.get('candidate_correct')
            if a is None or type(judgment) is not bool:raise StorageError('invalid_alignment','Unmatched candidate needs a human correctness verdict')
            ck=(item['article_id'],item['candidate_id'])
            if ck in human_judgments and human_judgments[ck]!=judgment:raise StorageError('conflicting_alignment','Contradictory human verdicts')
            human_judgments[ck]=judgment;continue
        case=cases.get(item['gold_id'])
        if case is None or case['article_id']!=item['article_id']:raise StorageError('invalid_alignment','Unknown gold/source pair')
        a=variants['A']['claims'].get(item['article_id'],{}).get(item['candidate_id'])
        if a is None:raise StorageError('invalid_alignment','Alignment must identify an accepted A candidate')
        matched.add((item['article_id'],item['gold_id']))
        kept=item['candidate_id'] in variants['B']['claims'].get(item['article_id'],{})
        judgment=item.get('candidate_correct')
        if judgment is not None:
            if type(judgment) is not bool:raise StorageError('invalid_alignment','candidate_correct is a human boolean verdict')
            ck=(item['article_id'],item['candidate_id'])
            if ck in human_judgments and human_judgments[ck]!=judgment:raise StorageError('conflicting_alignment','Contradictory human candidate verdicts')
            human_judgments[ck]=judgment
            if judgment:
                recalled_a.add(item['gold_id'])
                if kept:recalled_b.add(item['gold_id'])
        for path,expected in case['fields'].items():
            if path in item.get('ignored_paths',[]):continue
            claim=a.claim.model_dump(mode='json')
            try:actual=a.event_type if path=='/event_type' else pointer(claim,path)
            except (KeyError,IndexError,ValueError,TypeError):actual=None
            field_results.append({'article_id':item['article_id'],'candidate_id':item['candidate_id'],'gold_id':item['gold_id'],
                'path':path,'expected':expected,'actual':actual,'matches':contains(actual,expected),
                'kept_in_B':kept,'gold_ambiguities':case.get('ambiguities',[])})
    if reviewed_missing & {gid for _,gid in matched}:raise StorageError('conflicting_alignment','Gold case is both matched and missed')
    recalled_a={item['gold_id'] for item in mappings if item.get('gold_id') and item.get('candidate_id') and human_judgments.get((item['article_id'],item['candidate_id'])) is True}
    recalled_b={item['gold_id'] for item in mappings if item.get('gold_id') and item.get('candidate_id') and item['gold_id'] in recalled_a and item['candidate_id'] in variants['B']['claims'].get(item['article_id'],{})}
    for key,judgment in human_judgments.items():
        if key not in verification_decisions:review_counts['not_verified']+=1
        else:
            approved=verification_decisions[key]
            review_counts[('true_accept' if judgment else 'false_accept') if approved else ('false_reject' if judgment else 'true_reject')]+=1
    stats={}
    all_a={(article,cid) for article,cs in variants['A']['claims'].items() for cid in cs}
    precision_complete=all_a<=set(human_judgments)
    recall_complete=bool(cases) and set(cases)==({gid for _,gid in matched}|reviewed_missing) and precision_complete
    for variant in ('A','B'):
        fs=[r for r in field_results if variant=='A' or r['kept_in_B']]
        correct=sum(r['matches'] for r in fs)
        variant_keys={(article,cid) for article,cs in variants[variant]['claims'].items() for cid in cs}
        recalled=recalled_a if variant=='A' else recalled_b
        stats[variant]={'evaluated_fields':len(fs),'correct_fields':correct,'field_accuracy':correct/len(fs) if fs else None,
            'aligned_gold_cases':len({r['gold_id'] for r in fs}),
            'candidate_recall':len(recalled)/len(cases) if recall_complete else None,
            'precision':sum(human_judgments[k] for k in variant_keys)/len(variant_keys) if precision_complete and variant_keys else None,
            'metrics_note':'Recall/precision require exhaustive adjudicated alignment; unmapped claims are not negatives.'}
    calls=calls or []
    usage=Counter();elapsed=0;by_stage={k:Counter() for k in ('shared_A','B_incremental','capabilities','other')}
    for c in calls:
        payload=c.get('response') or {};body=payload.get('body',{})
        u=body.get('usage') or {}
        request=c.get('request') or {};messages=request.get('messages') or [{}]
        prompt=messages[0].get('content','')
        stage='shared_A' if prompt.startswith((prompts.DISCOVERY,prompts.EXTRACTION)) else 'B_incremental' if prompt.startswith(prompts.VERIFICATION) else 'capabilities' if prompt.startswith('FORMAT_PROBE_V2') else 'other'
        by_stage[stage]['requests']+=1
        for k in ('prompt_tokens','completion_tokens','total_tokens'):
            if type(u.get(k)) is int:usage[k]+=u[k];by_stage[stage][k]+=u[k]
        elapsed+=c.get('elapsed_seconds') or 0
    result={'schema_version':'extraction-comparison.v1','status':'human_aligned_draft' if mappings else 'awaiting_human_alignment',
        'variants':{k:{'articles':v['articles'],'metrics':stats[k]} for k,v in variants.items()},
        'field_results':field_results,'verification_confusion':dict(review_counts),
        'gold_sha256':digest(gold) if gold is not None else None,'alignments_sha256':digest(mappings),
        'reviewed_missing_gold_cases':sorted(reviewed_missing),'gold_cases':len(cases),'aligned_gold_cases':len(matched),'unmatched_gold_cases':sorted(set(cases)-{gid for _,gid in matched}),
        'requests':len(calls),'usage':dict(usage),'usage_by_stage':{k:dict(v) for k,v in by_stage.items()},'elapsed_seconds':elapsed,'cost_rub':None,
        'limitations':['Model verification does not prove source truth.','Development set; no generalization estimate.',
                      'B reuses A; shared extraction cost must not be counted twice.']}
    return result


def export_comparison(path,report,*,calls=None,bundles=None,inputs=None):
    """Local report includes complete records; no API call, no provider credentials."""

    from uuid import uuid4
    path=Path(path);path.mkdir(parents=True,exist_ok=True)
    for name,payload in [('comparison.json',report),('calls.json',calls or []),('bundles.json',bundles or {}),('inputs.json',inputs or [])]:
        temporary=path/('.'+name+'.'+str(uuid4())+'.tmp')
        temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n');temporary.replace(path/name)
    text=['# A/B comparison','',f"Status: {report['status']}",'', '| Variant | Accepted B4 | Evaluated fields | Field accuracy |','|---|---:|---:|---:|']
    for variant,data in report['variants'].items():
        text.append(f"| {variant} | {sum(a['accepted'] for a in data['articles'])} | {data['metrics']['evaluated_fields']} | {data['metrics']['field_accuracy']} |")
    text+=['',f"Requests: {report['requests']}; tokens: {report['usage']}",'',
           'Unaligned records are not counted as incorrect. Counts alone are not extraction quality.',
           'Full inputs, calls, candidate diagnostics and B6 bundles are in adjacent JSON files.']
    temporary=path/('.RESULTS.'+str(uuid4())+'.tmp');temporary.write_text('\n'.join(text)+'\n');temporary.replace(path/'RESULTS.md')
    return {'output_path':str(path),'status':'exported'}
