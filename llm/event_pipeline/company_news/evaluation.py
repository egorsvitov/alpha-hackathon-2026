"""Sparse, explicitly judged-pool metrics. Unjudged is never a negative."""
from collections import defaultdict
from pathlib import Path
import random
import numpy as np
from .io import rows, read, save, save_rows, require, load_plan, complete, sha


def make_review_pool(root, *, split='dev', depth=1):
    root=Path(root);load_plan(root);require(root,'retrieve')
    splits={r['document_id']:r for r in rows(root/'story-split.jsonl')}
    inputs=rows(root/'retrieval.jsonl')
    if (root/'rerank.stage.json').exists():inputs+=rows(root/'reranking.jsonl')
    docs={d['reference']['document_id']:d for d in rows(root/'documents.jsonl')};bycompany=defaultdict(set);provenance=defaultdict(list)
    union=defaultdict(set)
    for r in inputs:
        uid=r['company_id'];eligible=[c for c in r['candidates'][:100] if splits[c['document_id']]['split']==split]
        for c in r['candidates'][:100]:union[uid].add(c['document_id'])
        for c in eligible[:depth]:
            bycompany[uid].add(c['document_id']);provenance[uid,c['document_id']].append(r['variant'])
    rng=random.Random(20261023+(split=='test'))
    result=[]
    for uid in sorted(bycompany):
        outside=sorted(set(docs)-union[uid]);outside=[d for d in outside if splits[d]['split']==split]
        selected=set(bycompany[uid])
        if outside:
            audit=rng.choice(outside);selected.add(audit);provenance[uid,audit]=['random_outside_pool']
        for did in sorted(selected):result.append(dict(company_id=uid,document_id=did,split=split,story_id=splits[did]['story_id'],
            sampling=provenance[uid,did],label=None,importance=None,path=None,status=None,rationale=None,
            source_hash=docs[did]['reference']['content_hash'],reviewer='agent-manual-source-review-not-independent-human-gold'))
    path=root/('review-pool-'+split+'.jsonl')
    if path.exists():raise FileExistsError('frozen_review_pool_exists')
    save_rows(path,result)
    return dict(pairs=len(result),companies=len(bycompany),file=str(path))


def evaluate(root, *, split='dev', labels_path=None):
    root=Path(root);load_plan(root);require(root,'retrieve')
    labels_path=Path(labels_path or root/('judgments-'+split+'.jsonl'));labels=rows(labels_path)
    docs={d['reference']['document_id']:d for d in rows(root/'documents.jsonl')};stories={d['document_id']:d for d in rows(root/'story-split.jsonl')}
    if len({(r['company_id'],r['document_id']) for r in labels})!=len(labels):raise ValueError('duplicate_judgment')
    for j in labels:
        if j['label'] not in ('send','reject','uncertain') or not j['rationale'] or j['source_hash']!=docs[j['document_id']]['reference']['content_hash'] or stories[j['document_id']]['split']!=j['split']:raise ValueError('invalid_or_stale_judgment')
    labels=[j for j in labels if j['split']==split];bycompany=defaultdict(dict)
    for j in labels:bycompany[j['company_id']][j['document_id']]=j
    ranking=rows(root/'retrieval.jsonl')
    reranked={}
    if (root/'rerank.stage.json').exists():
        extra=rows(root/'reranking.jsonl');ranking+=extra;reranked={r['company_id']:r['candidates'] for r in extra}
    selections={}
    if (root/'select.stage.json').exists():
        require(root,'select')
        for row in rows(root/'selected.jsonl'):
            b=row['batch'];selections[b['company_id'],row['variant']]=[c['document_id'] for c in b['selected_context']]
            if row['variant'] in ('G4','R0','R4','R8'):
                ranking.append(dict(company_id=b['company_id'],variant=row['variant'],candidates=reranked[b['company_id']]))
    metrics=defaultdict(list);misses=[]
    for r in ranking:
        uid=r['company_id'];js=bycompany.get(uid,{})
        if not js:continue
        ids=[c['document_id'] for c in r['candidates']];positives={did for did,j in js.items() if j['label']=='send'}
        selected=selections.get((uid,r['variant']))
        record=dict(company_id=uid,known_positives=len(positives),judged=len(js))
        for k in (20,50,100):
            top=[did for did in ids[:k] if stories[did]['split']==split];judged=[js[did] for did in top if did in js];binary=[j for j in judged if j['label']!='uncertain']
            record['recall@'+str(k)]=len(set(top)&positives)/len(positives) if positives else None
            record['labelled_precision@'+str(k)]=sum(j['label']=='send' for j in binary)/len(binary) if binary else None
            record['judgment_coverage@'+str(k)]=len(judged)/len(top) if top else 0.
            record['unknown@'+str(k)]=sum(did not in js or js[did]['label']=='uncertain' for did in top)
        if selected is not None:
            known=[js[did] for did in selected if did in js and js[did]['label']!='uncertain'];got=set(selected)&positives
            record.update(final_recall=len(got)/len(positives) if positives else None,final_labelled_precision=sum(j['label']=='send' for j in known)/len(known) if known else None,
                final_judged=len(known),final_documents=len(selected),useful_stories=len({stories[d]['story_id'] for d in got}),
                important_misses=sum(j['label']=='send' and j['importance']=='high' and did not in selected for did,j in js.items()))
            for did in positives-set(selected):misses.append(dict(company_id=uid,variant=r['variant'],document_id=did,importance=js[did]['importance'],stage='retrieval' if did not in ids[:100] else 'final_selection'))
        metrics[r['variant']].append(record)
    summaries={}
    for variant,records in metrics.items():
        numeric=sorted({k for r in records for k,v in r.items() if k!='company_id' and isinstance(v,(float,int))})
        summaries[variant]={k:float(np.mean([r[k] for r in records if r.get(k) is not None])) for k in numeric}
        summaries[variant]['companies']=len(records)
        summaries[variant]['positive_companies']=sum(r['known_positives']>0 for r in records)
    result=dict(split=split,labels_sha256=sha(labels_path),judged_pairs=len(labels),labels=dict((k,sum(j['label']==k for j in labels)) for k in ('send','reject','uncertain')),
        limitations=['agent_manual_not_independent_human_gold','sparse_system_pool_plus_random_audit','recall_only_known_positive_pool_not_corpus_recall','unlabelled_not_negative','profile_history_unproven'],
        variants=summaries,per_company=metrics,misses=misses)
    save(root/('evaluation-'+split+'.json'),result)
    return result
