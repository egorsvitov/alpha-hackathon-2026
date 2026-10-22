"""Offline B5 checks on hand-authored claims; never an extraction benchmark."""
from itertools import combinations
import json
from pathlib import Path
from llm.events.catalog.models import CatalogConfig, ClaimContext
from llm.events.catalog.search import pair_key
from llm.events.catalog.grouping import cluster_claims
from llm.events.catalog.matching import compare_claims
from llm.events.storage.store import StorageError
from llm.events.contracts.validation import validate_gold, validate_claim_collection


def check_fixtures(path):
    document=json.loads(Path(path).read_text());reports=[]
    for case in document['cases']:
        contexts={c['claim']['claim_id']:ClaimContext.model_validate(c) for c in case['contexts']}
        validate_claim_collection([c.claim for c in contexts.values()],[c.source for c in contexts.values()],
            [e for c in contexts.values() for e in c.evidence],case['articles'])
        groups,decisions,_=cluster_claims(contexts,CatalogConfig())
        errors=[]
        for expected in case['expected_pairs']:
            key=pair_key(*expected['pair'])
            decision=compare_claims(contexts[key[0]],contexts[key[1]])
            for field in ('identity','content'):
                if field in expected and getattr(decision,field)!=expected[field]:errors.append(field+':'+str(key))
        if sorted(groups)!=sorted(sorted(g) for g in case['expected_groups']):errors.append('groups')
        reports.append({'name':case['name'],'failed_checks':errors})
    if any(r['failed_checks'] for r in reports):raise StorageError('catalog_fixture_mismatch',json.dumps(reports,ensure_ascii=False))
    return {'status':'passed','cases':len(reports),'report':reports}


def evaluate_gold(path,corpus=None):
    gold=validate_gold(json.loads(Path(path).read_text()),json.loads(Path(corpus).read_text()) if corpus else None)
    contexts={}
    for bundle in gold.bundles:
        sources={s.source_id:s for s in bundle.sources};evidence={e.evidence_id:e for e in bundle.evidence}
        for claim in bundle.claims:
            ev=[evidence[eid] for eid in claim.evidence_ids]
            contexts[claim.claim_id]=ClaimContext(artifact_id='gold:'+claim.claim_id,candidate_id=claim.claim_id,
                event_type=bundle.event.event_type,claim=claim,source=sources[ev[0].source_id],evidence=ev,
                limitations=bundle.limitations)
    groups,decisions,candidates=cluster_claims(contexts,CatalogConfig())
    owner={cid:i for i,g in enumerate(groups) for cid in g}
    known=[l for l in gold.expected_links if l.relation!='uncertain']
    positives=[l for l in known if l.relation in ('same_event','duplicate_claim','conflicts_with')]
    def metric(tp,fp,fn):
        return {'tp':tp,'fp':fp,'fn':fn,'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None}
    scores={}
    for name in ('identity','duplicate_claim','conflicts_with'):
        tp=fp=fn=0
        for link in known:
            key=pair_key(link.from_claim_id,link.to_claim_id)
            d=decisions.get(key)
            expected=link.relation in ('same_event','duplicate_claim','conflicts_with') if name=='identity' else link.relation==name
            predicted=owner[key[0]]==owner[key[1]] if name=='identity' else d is not None and d.content==name and owner[key[0]]==owner[key[1]]
            tp+=int(expected and predicted);fp+=int(not expected and predicted);fn+=int(expected and not predicted)
        scores[name]=metric(tp,fp,fn)
    assignments={a.claim_id:a.expected_event_id for annotation in gold.annotations for a in annotation.claims}
    merged=[(a,b) for group in groups for a,b in combinations(group,2) if assignments.get(a) and assignments.get(b)]
    false_merges=sum(assignments[a]!=assignments[b] for a,b in merged)
    return {'status':'evaluated','dataset_kind':gold.dataset_kind,'annotation_status':gold.annotation_status,
        'articles':len(gold.articles),'claims':len(contexts),'labeled_pairs':len(gold.expected_links),
        'candidate_recall':sum(pair_key(l.from_claim_id,l.to_claim_id) in candidates for l in positives)/len(positives) if positives else None,
        'metrics':scores,'evaluated_candidate_pairs':len(decisions),
        'labeled_pair_results':[{'from_id':l.from_claim_id,'to_id':l.to_claim_id,'expected':l.relation,
            'retrieved':pair_key(l.from_claim_id,l.to_claim_id) in candidates,
            'same_group':owner[l.from_claim_id]==owner[l.to_claim_id],
            'decision':compare_claims(contexts[l.from_claim_id],contexts[l.to_claim_id]).model_dump(mode='json',exclude={'bases'})} for l in gold.expected_links],
        'uncertain_fraction':sum(d.identity=='uncertain' for d in decisions.values())/len(decisions) if decisions else None,
        'false_merges':false_merges,'known_merged_pairs':len(merged),
        'note':'Evaluation on supplied normalized gold claims, not B3 or end-to-end quality. Unlabeled pairs are not negatives.'}
