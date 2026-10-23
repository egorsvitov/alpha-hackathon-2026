"""Run all frozen views against the same corpus; unknown labels stay unknown."""
import argparse
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import json
from .dataset import load_dataset
from .runner import MATRIX,EvaluationRunner,run_variant
from .report import compare_runs
from ...company_build import CompanyEmbeddingSnapshot
from ...embedding_cache import VectorCache
from ...index import EventIndex
from ...service import RetrievalService
from ...representation_contracts import VariantConfig

def main():
    p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);p.add_argument('--index',required=True);p.add_argument('--snapshot',required=True);p.add_argument('--cache',required=True);p.add_argument('--output',required=True)
    p.add_argument('--split',default='diagnostic');p.add_argument('--temporal-mode',choices=['strict','static_context'],default='strict');p.add_argument('--weight-policy',choices=['equal','amount_share','sqrt_share'],default='sqrt_share')
    a=p.parse_args();dataset=load_dataset(a.dataset);index=EventIndex.open(a.index)
    if set(index.snapshots)!=set(dataset.manifest['splits'][a.split]):raise ValueError('index must contain exactly split universe')
    with closing(VectorCache(a.cache)) as cache,closing(CompanyEmbeddingSnapshot.open(a.snapshot,cache=cache)) as snapshot:
        runner=EvaluationRunner(RetrievalService(index),snapshot);runs=[]
        for variant,(views,hybrid) in MATRIX.items():
            config=VariantConfig(variant,views,hybrid,encoder_fingerprint=index.fingerprint,index_snapshot_id=index.index_snapshot_id,corpus_hash=dataset.dataset_hash,profile_snapshot_id=snapshot.snapshot_id,split=a.split,role=dataset.manifest['role'],as_of=index.as_of,window_start=index.window_start,window_end=index.window_end,temporal_mode=a.temporal_mode,graph_weight_policy=a.weight_policy)
            runs.append(run_variant(dataset,retrieval_port=runner,config=config))
    result=compare_runs(runs);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'quality_status':result['quality_status'],'variants':len(runs),'output':a.output}))

if __name__=='__main__':main()
