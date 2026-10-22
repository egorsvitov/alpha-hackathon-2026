"""Evaluation uses the production retrieval service, including its budget."""
from dataclasses import replace
from .metrics import evaluate_query
from ...company_queries import load_company_queries
from ...representation_contracts import QueryPolicy,GraphPolicy,BASE_VIEWS

MATRIX={'D1':(('universal',),False),'D3':(BASE_VIEWS,False),'H1':(('universal',),True),'H3':(BASE_VIEWS,True),
        'H3-I':(BASE_VIEWS+('graph_incoming',),True),'H3-O':(BASE_VIEWS+('graph_outgoing',),True),'H3-IO':(BASE_VIEWS+('graph_incoming','graph_outgoing'),True)}

class EvaluationRunner:
    def __init__(self, service, snapshot):self.service,self.snapshot=service,snapshot
    def run(self,request,*,company_queries,config):return self.service.run(request,company_queries=company_queries,config=config)
    def queries(self,company_id,config):
        directions=tuple(k.removeprefix('graph_') for k in config.dense_views if k.startswith('graph_'))
        policy=QueryPolicy(config.dense_views,bool(directions),GraphPolicy(bool(directions),directions,config.graph_weight_policy),config.temporal_mode)
        return load_company_queries(self.snapshot,company_id=company_id,expected_encoder_fingerprint=self.service.index.fingerprint,policy=policy,as_of=config.as_of)

def run_variant(dataset, *, retrieval_port, config):
    if config.variant_id not in MATRIX:raise ValueError('unknown frozen variant')
    if (config.dense_views,config.hybrid)!=MATRIX[config.variant_id]:raise ValueError('variant settings mismatch')
    if config.corpus_hash!=dataset.dataset_hash or config.role!=dataset.manifest['role']:raise ValueError('dataset/config mismatch')
    if config.split not in dataset.manifest['splits']:raise ValueError('unknown split')
    if dataset.manifest.get('context_snapshot_id') and config.profile_snapshot_id!=dataset.manifest['context_snapshot_id']:
        raise ValueError('evaluation context snapshot differs')
    universe=set(dataset.manifest['splits'][config.split]);rows=[]
    for q in dataset.queries:
        uid=q['company_id'];request=dict(q,profile=dataset.profiles[uid])
        queries=retrieval_port.queries(uid,config)
        result=retrieval_port.run(request,company_queries=queries,config=config)
        output=[c['members'][0]['snapshot']['event_id'] for c in result['candidate_batch']['candidates']]
        if not set(output)<=universe:raise ValueError('index universe differs from evaluation split')
        labels=[j for j in dataset.judgments if j['company_id']==uid and j['event_id'] in universe]
        if {j['event_id'] for j in labels}!=universe:raise ValueError('judgment form must cover full universe, including nulls')
        metrics=evaluate_query(output,labels)
        rows.append(dict(company_id=uid,selected=output,metrics=metrics,latency_seconds=result['trace']['duration_seconds'],token_estimate=result['trace']['token_estimate'],omitted=queries.omitted))
    return {'variant_id':config.variant_id,'dataset_hash':dataset.dataset_hash,'config':config.to_dict(),'queries':rows,'quality_status':'evaluated' if dataset.manifest.get('split_status')=='human_reviewed_frozen' and rows and all(r['metrics']['quality_status']=='evaluated' for r in rows) else 'not_evaluated'}
