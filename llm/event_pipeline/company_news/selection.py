"""Bounded source selection; reading groups never establish event identity."""
from collections import defaultdict
from pathlib import Path
from .contracts import RawNewsCandidateBatch, CandidateTrace, ContextSelection, SourceSpan
from .experiment import eligible
from .io import rows, read, save, save_rows, load_plan, require, complete


def reserve_order(reranked, original, reserve):
    ids={r['document_id'] for r in original[:reserve]}
    # Guarantee reserved originals are considered within the same final document/byte budget.
    return original[:reserve]+[r for r in reranked if r['document_id'] not in ids]


def choose_context(ordered, docs, groups, *, as_of, max_documents=20, max_bytes=24000, grouped=False):
    if max_documents<1 or max_bytes<1:raise ValueError('incomplete_context_budget')
    membership=defaultdict(list)
    for g in groups:
        for uid in g['document_ids']:membership[uid].append(g['group_id'])
    chosen=[];used=set();body_hashes=set();cost=0;exclusions=[]
    def add(uid,role,parent=None):
        nonlocal cost
        d=docs[uid];size=len(d['text'].encode())
        reason=eligible(d,as_of)
        if uid in used:return False
        if not reason and grouped and d['reference']['content_hash'] in body_hashes:reason='exact_body_duplicate'
        if not reason and len(chosen)>=max_documents:reason='final_document_cap'
        if not reason and cost+size>max_bytes:reason='context_byte_budget'
        if reason:
            exclusions.append(dict(document_id=uid,stage='selection',reason=reason));return False
        chosen.append(ContextSelection(document_id=uid,role=role,parent_document_id=parent,group_ids=sorted(membership[uid]),
            spans=[SourceSpan(document_id=uid,content_hash=d['reference']['content_hash'],start=0,end=len(d['text']))]).model_dump())
        used.add(uid);body_hashes.add(d['reference']['content_hash']);cost+=size
        return True
    for r in ordered:add(r['document_id'],'retrieved')
    # Expansion only uses spare budget, never displaces directly found documents.
    if grouped and len(chosen)<max_documents:
        directly_found={r['document_id'] for r in ordered}
        for parent in list(chosen):
            for g in groups:
                if g['group_id'] not in parent['group_ids']:continue
                neighbors=[uid for uid in g['document_ids'] if uid in docs and uid not in directly_found]
                # Preserve source/date differences as candidates; no inherited retrieval score.
                neighbors.sort(key=lambda uid:(docs[uid]['reference']['published_at']==docs[parent['document_id']]['reference']['published_at'],uid))
                for uid in neighbors:
                    if add(uid,'context_expansion',parent['document_id']):break
    return chosen,exclusions,cost


def select(root, *, reserve=0):
    root=Path(root);m=load_plan(root);require(root,'retrieve');require(root,'rerank')
    docs={d['reference']['document_id']:d for d in rows(root/'documents.jsonl')};queries=rows(root/'queries.jsonl');groups=rows(root/'groups.jsonl')
    retr=rows(root/'retrieval.jsonl');rer=rows(root/'reranking.jsonl');base=read(root/'rerank-choice.json')['base_variant']
    lookup={(r['company_id'],r['variant']):r for r in retr}
    allrows=retr+rer+[dict(r,variant='G4') for r in rer]
    # Dev reserve ablations keep the same byte and document limits.
    allrows += [dict(r,variant='R'+str(n),reserve=n) for n in (0,4,8) for r in rer]
    batches=[];diagnostics=[]
    for row in allrows:
        uid=row['company_id'];variant=row['variant'];pool=row['candidates'][:100];order=pool
        if variant in ('G3','G4','R0','R4','R8'):
            order=reserve_order(pool,lookup[uid,base]['candidates'][:100],row.get('reserve',reserve))
        selected,excluded,cost=choose_context(order,docs,groups,as_of=m['as_of'],max_documents=m['budget']['final_documents'],max_bytes=m['budget']['context_utf8_bytes'],grouped=variant=='G4')
        chosen={c['document_id'] for c in selected};included={r['document_id'] for r in pool}|chosen
        traces=[]
        ereasons={e['document_id']:e['reason'] for e in excluded}
        for r in pool:
            fields={k:r[k] for k in ('document_id','hits','fusion_score','fusion_rank','rerank_score','rerank_rank','best_query_id','reranker_status') if k in r}
            fields.update(selected=r['document_id'] in chosen,exclusion_stage=None if r['document_id'] in chosen else 'selection',exclusion_reason=ereasons.get(r['document_id']))
            traces.append(CandidateTrace(**fields))
        qs=[q for q in queries if q['company_id']==uid]
        usedqueries={h['query_id'] for c in pool for h in c['hits']}
        batch=RawNewsCandidateBatch(company_id=uid,profile_version=m['profile_versions'][uid],as_of=m['as_of'],snapshot_id=m['snapshot_id'],
            queries=[q for q in qs if q['query_id'] in usedqueries],documents=[docs[u] for u in sorted(included)],candidates=traces,selected_context=selected,
            budget=dict(m['budget'],actual_context_bytes=cost,actual_documents=len(selected),reserve=row.get('reserve',reserve)),
            limitations=['static_company_snapshot_historical_availability_unproven','reading_groups_are_not_event_identity','retrieval_is_not_proven_company_impact'])
        batches.append(dict(variant=variant,batch=batch.model_dump()))
        diagnostics.append(dict(company_id=uid,variant=variant,selected_documents=len(selected),context_bytes=cost,estimated_tokens=(cost+3)//4,
            expansion=sum(c['role']=='context_expansion' for c in selected),exclusions=excluded))
    save_rows(root/'selected.jsonl',batches);save_rows(root/'selection-diagnostics.jsonl',diagnostics)
    complete(root,'select',[root/'selected.jsonl',root/'selection-diagnostics.jsonl'],dict(batches=len(batches),final_documents=m['budget']['final_documents'],context_utf8_bytes=m['budget']['context_utf8_bytes'],reserve=reserve))
    return dict(batches=len(batches))
