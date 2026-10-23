"""Company → event candidates, preserving originals for a separate analyst."""
import time
from decimal import Decimal
import copy
from ranking.contracts import payload_hash, parse_timestamp
from .representation_contracts import EncoderMismatch, content_hash
from .channels.dense import search_dense
from .channels.lexical import search_lexical
from .channels.structured import search_structured
from .fusion import fuse, estimate_tokens, VERSION, COUNTER_VERSION
from .contracts import validate_candidate_batch, SCHEMA_VERSION, ROLES

class RetrievalService:
    def __init__(self, index, *, entity_mapping=None):
        self.index=index;self.entity_mapping=entity_mapping

    def run(self, request, *, company_queries, config):
        started=time.perf_counter();index=self.index;profile=request['profile'];queries=company_queries
        if queries.company_id!=profile['company_id'] or queries.profile_version!=profile['profile_version']:
            raise ValueError('cached company/profile mismatch')
        if request['role'] not in ROLES or config.role!=request['role']:raise ValueError('role mismatch')
        for name in ('as_of','window_start','window_end'):
            if parse_timestamp(request[name])!=parse_timestamp(getattr(index,name)):raise ValueError('index cutoff mismatch')
            if getattr(config,name) and parse_timestamp(getattr(config,name))!=parse_timestamp(request[name]):raise ValueError('variant cutoff mismatch')
        if config.encoder_fingerprint and config.encoder_fingerprint!=index.fingerprint:raise EncoderMismatch('variant/index mismatch')
        if config.index_snapshot_id and config.index_snapshot_id!=index.index_snapshot_id:raise ValueError('wrong index snapshot')
        if config.profile_snapshot_id and config.profile_snapshot_id!=queries.snapshot_id:raise ValueError('wrong company snapshot')
        if {v.kind for v in queries.queries}-set(config.dense_views):raise ValueError('unexpected query views')
        if config.temporal_mode!=queries.temporal_mode:raise ValueError('temporal mode mismatch')
        channel_timings={};channel_start=time.perf_counter()
        dense=search_dense(index,queries,top_k=config.top_k_per_view)
        channel_timings['dense']=time.perf_counter()-channel_start;channel_start=time.perf_counter()
        # Lexical inputs ALWAYS base3, including universal/graph ablations.
        lexical_views=[v for v in queries.queries if v.kind in {'business','finance','trade_geo','universal'}]
        # Universal is the exact concatenation of stored base texts. BM25 uses
        # a set of query terms, so D1/H1 and D3/H3 get identical lexical inputs.
        lexical=search_lexical(index,[v.metadata_ref['text'] for v in lexical_views],top_k=config.top_k_per_view) if config.hybrid else []
        for hit in lexical:
            hit['query_feature_refs']=[r.to_dict() for v in lexical_views for r in v.feature_refs]
            hit['feature_ids']=sorted({r.feature_id for v in lexical_views for r in v.feature_refs})
        channel_timings['lexical']=time.perf_counter()-channel_start;channel_start=time.perf_counter()
        structured=search_structured(index,profile,top_k=config.top_k_per_view,entity_mapping=self.entity_mapping) if config.hybrid else []
        channel_timings['structured']=time.perf_counter()-channel_start
        records=fuse(dense,lexical,structured,config=config)
        payment=queries.payment_context_ref
        # Same compact evidence context for all variants. Full edges remain in the
        # trace artifact, with an exact hash; never describe a selected subset as all.
        if payment:
            payment=copy.deepcopy(payment)
            full_neighborhood=payment['neighborhood']
            payment['full_context_ref']={'trace_id':None,'json_path':'/company_queries/payment_context_ref',
                'sha256':content_hash(queries.payment_context_ref)}
            payment['selected_edges_policy']='top5-observed-amount-per-direction.v1'
            payment['omitted_neighbor_counts']={}
            for direction in ('incoming','outgoing'):
                all_edges=full_neighborhood[direction]
                full_neighborhood[direction]=sorted(all_edges,key=lambda e:(-Decimal(e['amount_raw']),e['company_id']))[:5]
                payment['omitted_neighbor_counts'][direction]=max(0,len(all_edges)-5)
            payment['limitations']=['selected_edges_are_not_complete_graph; full context in trace artifact']
        facts=[v for _,v in sorted(profile['features'].items()) if v.get('quality')=='observed' and not v['path'].startswith('/graph/')]
        context=dict(profile_version=profile['profile_version'],profile_hash=payload_hash(profile),snapshot=profile['source_snapshot'],
            verified_facts=facts,derived_exposures=[],group_context={'status':'not_loaded'},
            payment_context=dict(status='available',**payment) if payment else {'status':'not_loaded'},
            unknowns=[k for k,v in sorted(profile['features'].items()) if v.get('quality') in {'unknown','conflicting'}])
        request_public={k:request[k] for k in ('request_id','company_id','role','as_of','window_start','window_end')}
        request_public.update(requested_limit=config.output_limit,max_input_tokens=config.token_budget)
        trace_id=content_hash({'request':request_public,'profile':context['profile_hash'],'index':index.index_snapshot_id,'queries':queries.to_dict(),'config':config,'entity_mapping_hash':content_hash(self.entity_mapping)})
        if payment:payment['full_context_ref']['trace_id']=trace_id
        batch=dict(schema_version=SCHEMA_VERSION,request=request_public,status='empty',empty_reason=None,company_context=context,candidates=[],
            retrieval_context=dict(trace_id=trace_id,index_snapshot_id=index.index_snapshot_id,encoder_fingerprint=index.fingerprint,
                representation_version='company-views.v1',policy_version=VERSION,data_watermark=index.as_of,failed_channels=[]),
            limitations=list(dict.fromkeys(queries.limitations+['profile_data_as_of_unknown','token_count_is_utf8_proxy_not_model_tokenizer','retrieval_similarity_is_not_business_significance'])))
        for record in records:
            if record['omission_reason']:continue
            if len(batch['candidates'])>=config.output_limit:record['omission_reason']='output_limit';continue
            stored=index.snapshots[record['event_id']];claims=[];paths=[];fids=[];neighbor_refs=[];views=[];channels=[]
            for hit in record['hits']:
                channels.append(hit['channel'])
                if hit['channel']=='dense':
                    views.append(hit['view_kind']);claims.extend(hit['chunk_ref']['claim_refs']);paths.extend(o['path'] for o in hit['chunk_ref']['offsets'])
                    for ref in hit['company_feature_refs']:
                        if ref['company_id']!=profile['company_id'] or ref['profile_version']!=profile['profile_version']:raise ValueError('foreign base feature')
                        fids.append(ref['feature_id'])
                    allowed_neighbors={n['company_id'] for d in ('incoming','outgoing') for n in (payment or {}).get('neighborhood',{}).get(d,[])}
                    for inp in hit['view_ref'].get('input_vector_refs',[]):
                        if inp['company_id'] in allowed_neighbors:neighbor_refs.extend(inp['feature_refs'])
                else:
                    fids.extend(hit.get('feature_ids',[]));paths.extend(hit.get('event_paths',[]));claims.extend(hit.get('claim_refs',[]))
            unique=lambda values: list({content_hash(v):v for v in values}.values())
            candidate=dict(candidate_id=content_hash([stored.snapshot.to_dict(),'single-event.v1']),event_cluster_id=None,cluster_version=None,
                members=[{'snapshot':stored.snapshot.to_dict(),'bundle':stored.bundle}],
                match_context=dict(channels=sorted(set(channels)),matched_views=sorted(set(views)),feature_ids=sorted(set(fids)),
                    neighbor_feature_refs=unique(neighbor_refs),claim_refs=unique(claims),event_paths=sorted(set(paths)),hypotheses=[],unknowns=[]),
                limitations=['candidate_requires_business_analysis']+(['publication_unknown_window_uses_receipt'] if stored.bundle['event'].get('first_published_at') is None else [])+(['graph_match_uses_full_neighborhood; only_top_edges_in_analyst_projection'] if any(v.startswith('graph_') for v in views) else []))
            trial=dict(batch,candidates=batch['candidates']+[candidate],status='ready',empty_reason=None)
            # Count the entire analyst input, never silently truncate claims/evidence.
            if estimate_tokens(trial)>config.token_budget:record['omission_reason']='token_budget';continue
            batch=trial;record['selected']=True
        if not batch['candidates']:
            batch['empty_reason']='no_events' if not index.snapshots else 'no_matches' if not records else 'budget_exhausted'
        validate_candidate_batch(batch,profile=profile,index=index,payment_context_source=queries.payment_context_ref)
        return {'candidate_batch':batch,'trace':dict(trace_id=trace_id,events=records,config=config.to_dict(),company_queries=queries.to_dict(),
            token_estimate=estimate_tokens(batch),counter_version=COUNTER_VERSION,context_overflow=estimate_tokens(dict(batch,candidates=[]))>config.token_budget,
            duration_seconds=time.perf_counter()-started,channel_seconds=channel_timings,corpus_events=len(index.snapshots),selected=len(batch['candidates']))}
