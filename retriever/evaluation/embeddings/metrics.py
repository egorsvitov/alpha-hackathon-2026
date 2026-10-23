"""Metrics never turn missing or weak judgments into negative human labels."""
import math

def evaluate_query(selected_event_ids, judgments, *, k=20, relevant_min=2):
    selected=list(dict.fromkeys(selected_event_ids))[:k]
    valid={j['event_id']:j['label'] for j in judgments if j['status']=='judged' and j['label_source']=='human' and type(j['label']) is int and not j.get('synthetic',False)}
    complete=bool(judgments) and len(valid)==len(judgments) and all(e in valid for e in selected)
    relevant={e for e,v in valid.items() if v>=relevant_min};critical={e for e,v in valid.items() if v==3}
    hits=len(set(selected)&relevant);critical_hits=len(set(selected)&critical)
    ideal=sorted(valid.values(),reverse=True)[:10]
    idcg=sum((2**v-1)/math.log2(i+2) for i,v in enumerate(ideal))
    dcg=sum((2**valid.get(e,0)-1)/math.log2(i+2) for i,e in enumerate(selected[:10]))
    return dict(quality_status='evaluated' if complete else 'partial' if valid else 'not_evaluated',
        recall_at_20=hits/len(relevant) if complete and relevant else None,
        precision_at_20=hits/k if complete else None,precision_returned=hits/len(selected) if complete and selected else None,
        ndcg_at_10=dcg/idcg if complete and idcg else None,class3_recall=critical_hits/len(critical) if complete and critical else None,
        judged_pairs=len(valid),total_pairs=len(judgments),selected=len(selected),relevant_count=len(relevant),hits=hits,
        unjudged_selected=[e for e in selected if e not in valid])
