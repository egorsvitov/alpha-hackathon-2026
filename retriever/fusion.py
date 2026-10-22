"""One shared RRF implementation; additional dense views do not add channel weight."""
import json
import math

VERSION='max-dense-rrf.v1'
COUNTER_VERSION='utf8_bytes_div4.v1'

def estimate_tokens(value):
    return math.ceil(len(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())/4)

def fuse(dense, lexical, structured, *, config):
    if config.reserve_policy!='none' or config.counter_version!=COUNTER_VERSION:
        raise ValueError('unsupported retrieval policy')
    if not (0<config.output_limit<=20 and 0<config.pool_cap<=100 and config.pool_cap<=config.union_cap<=250 and config.token_budget>0 and config.fusion_k>0):
        raise ValueError('invalid retrieval limits')
    events={}
    for hit in [*[h.to_dict() for h in dense],*lexical,*structured]:
        uid=hit.get('event_id') or hit['snapshot']['event_id']
        record=events.setdefault(uid,{'event_id':uid,'hits':[],'contributions':{'dense':0.,'lexical':0.,'structured':0.}})
        record['hits'].append(hit)
        channel=hit['channel']
        record['contributions'][channel]=max(record['contributions'][channel],1/(config.fusion_k+hit['rank']))
    weights=dict(zip(('dense','lexical','structured'),config.channel_weights))
    for record in events.values(): record['score']=sum(v*weights[c] for c,v in record['contributions'].items())
    ordered=sorted(events.values(),key=lambda r:(-r['score'],r['event_id']))
    for i,r in enumerate(ordered):
        r['selected']=False
        r['omission_reason']='union_cap' if i>=config.union_cap else 'pool_cap' if i>=config.pool_cap else None
    return ordered
