"""Paired company-level bootstrap; no promotion from an unlabelled smoke run."""
import random
import statistics

def compare_runs(runs, *, bootstrap_seed=42, bootstrap_samples=2000):
    if not runs:raise ValueError('no runs')
    invariant=('encoder_fingerprint','index_snapshot_id','corpus_hash','profile_snapshot_id','split','role','as_of','window_start','window_end','token_budget','counter_version','fusion_k','channel_weights','temporal_mode','top_k_per_view','union_cap','pool_cap','output_limit','reserve_policy','graph_weight_policy')
    first=runs[0]
    for run in runs:
        if run['dataset_hash']!=first['dataset_hash'] or any(run['config'][k]!=first['config'][k] for k in invariant):raise ValueError('incomparable runs')
        if {q['company_id'] for q in run['queries']}!={q['company_id'] for q in first['queries']}:raise ValueError('different query companies')
    evaluated=all(r['quality_status']=='evaluated' for r in runs)
    report={'quality_status':'evaluated' if evaluated else 'not_evaluated','exploratory':True,'graph_default_enabled':False,'runs':runs,'paired':[]}
    report['summary']=[]
    for run in runs:
        lat=sorted(q['latency_seconds'] for q in run['queries'])
        metric_rows=[q['metrics'] for q in run['queries']]
        macro={key:statistics.mean(values) if (values:=[m[key] for m in metric_rows if m[key] is not None]) else None for key in ('recall_at_20','precision_at_20','precision_returned','ndcg_at_10','class3_recall')}
        positives=sum(m['relevant_count'] for m in metric_rows)
        report['summary'].append({'variant_id':run['variant_id'],'macro':macro,'micro_recall':sum(m['hits'] for m in metric_rows)/positives if evaluated and positives else None,
            'latency_p50_seconds':statistics.median(lat) if lat else None,'latency_p95_seconds':lat[min(len(lat)-1,int(.95*len(lat)))] if lat else None,
            'returned_events':sum(m['selected'] for m in metric_rows)})
    if not evaluated:return report
    baseline=next((r for r in runs if r['variant_id']=='H3'),first)
    base={q['company_id']:q['metrics']['recall_at_20'] for q in baseline['queries']}
    rng=random.Random(bootstrap_seed)
    for run in runs:
        diffs=[q['metrics']['recall_at_20']-base[q['company_id']] for q in run['queries'] if q['metrics']['recall_at_20'] is not None and base[q['company_id']] is not None]
        samples=sorted(statistics.mean(rng.choices(diffs,k=len(diffs))) for _ in range(bootstrap_samples)) if diffs else []
        report['paired'].append({'variant_id':run['variant_id'],'reference':baseline['variant_id'],'mean_recall_delta':statistics.mean(diffs) if diffs else None,
            'company_bootstrap_95': [samples[int(.025*len(samples))],samples[min(len(samples)-1,int(.975*len(samples)))]] if samples else None,
            'gains':sum(d>0 for d in diffs),'losses':sum(d<0 for d in diffs)})
    return report
