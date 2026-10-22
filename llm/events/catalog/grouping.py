"""Pure bounded complete-link grouping shared by catalog snapshots and evaluation."""
from llm.events.catalog.models import PairDecision
from llm.events.catalog.search import BM25CandidateSearch, pair_key
from llm.events.catalog.matching import compare_claims
from llm.events.storage.store import StorageError, canonical_json


def cluster_claims(contexts,config,reviews=None,search=None):
    reviews=reviews or {}
    search=search or BM25CandidateSearch()
    candidates=search.search(list(contexts.values()),config,reviews)
    decisions={}
    def compare(a,b):
        key=pair_key(a,b)
        if key in decisions:return decisions[key]
        if len(decisions)>=config.max_comparisons:
            raise StorageError('catalog_comparison_limit','Pair checks exceed max_comparisons')
        raw=compare_claims(contexts[key[0]],contexts[key[1]])
        if key in reviews:
            review=PairDecision.model_validate(reviews[key])
            if review.identity=='same_event' and raw.hard_differences:
                raise StorageError('review_incompatible_with_rules','Review conflicts with current explicit identity')
            raw=review
        retrieval=candidates.get(key,{'methods':{'cluster_check'},'score':0.0})
        raw=raw.model_copy(update={'retrieval_methods':sorted(retrieval['methods']),'retrieval_score':retrieval['score']})
        decisions[key]=raw
        return raw
    for a,b in sorted(candidates):compare(a,b)
    groups=[{cid} for cid in sorted(contexts)]
    def group_for(cid):return next(g for g in groups if cid in g)
    def merge(a,b,mandatory=False):
        ga,gb=group_for(a),group_for(b)
        if ga is gb:return
        blockers=[pair_key(x,y) for x in sorted(ga) for y in sorted(gb) if compare(x,y).identity!='same_event']
        if blockers:
            if mandatory:raise StorageError('incomplete_cluster_confirmation','Blocking pairs: '+canonical_json([list(k) for k in blockers]))
            return
        groups.remove(ga);groups.remove(gb);groups.append(ga|gb)
    for (a,b),r in sorted(reviews.items()):
        if r['identity']=='same_event':merge(a,b,mandatory=True)
    for key in sorted(candidates):
        if compare(*key).identity=='same_event':merge(*key)
    groups=sorted((sorted(g) for g in groups),key=lambda g:g[0])
    # Manual must-links must remain complete-link compatible as a whole.
    for (a,b),r in reviews.items():
        if r['identity']=='same_event' and group_for(a) is not group_for(b):
            raise StorageError('incomplete_cluster_confirmation','Manual same_event was not satisfied')
    return groups,decisions,candidates
