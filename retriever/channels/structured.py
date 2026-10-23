"""Explicit entity resolutions and conditional exposures, never significance scores."""
from ranking.entity_mapping import validate_entity_mapping,resolve
from ranking.contracts import parse_timestamp

VERSION='observed-attributes-conditional-exposures.v1'
EXPOSURES={'interest_rates':('loan','leasing','deposit'),'fx':(), 'exchange_rates':()}

def search_structured(index, profile, *, top_k=50, entity_mapping=None):
    mapping=validate_entity_mapping(entity_mapping)
    uid=profile['company_id'];features=profile['features'];hits=[]
    def observed(path,expected=None):
        f=features.get(uid+':'+path,{})
        return f.get('quality')=='observed' and f.get('value') is not None and (expected is None or f['value']==expected)
    for event_id,stored in sorted(index.snapshots.items()):
        rf=stored.bundle.get('ranking_features',{});fids=[];paths=[];basis=[];claims=[]
        if (observed('/industry') or (features.get(uid+':/industry',{}).get('quality')=='derived' and observed('/okved/code'))) and profile['industry'] in rf.get('industries',[]):
            fids.extend([uid+':/industry',uid+':/okved/code']);paths.append('/ranking_features/industries');basis.append({'kind':'normalized_industry','policy_version':VERSION,'normalization_version':profile['normalization_version']})
        for factor in sorted(set(rf.get('factors',[])) & EXPOSURES.keys()):
            matched=[uid+':/products/'+p for p in EXPOSURES[factor] if observed('/products/'+p,True)]
            if factor in {'fx','exchange_rates'} and observed('/fts_present',True):matched.append(uid+':/fts_present')
            if matched:
                fids.extend(matched);paths.append('/ranking_features/factors')
                basis.append({'kind':'conditional_exposure','factor':factor,'policy_version':VERSION,'assumptions':['event transmission to company is unverified']})
        if mapping:
            for i,claim in enumerate(stored.bundle['claims']):
                entities=[(f'/claims/{i}/participants/{j}',v) for j,v in enumerate(claim.get('participants',[]))]
                if claim.get('attribution'):entities.append((f'/claims/{i}/attribution',claim['attribution']))
                for path,entity in entities:
                    for entry in resolve(mapping,entity.get('entity_id'),as_of=parse_timestamp(index.as_of),source_mode=stored.source_mode,is_synthetic=profile.get('is_synthetic',False)):
                        if entry['company_id']==uid:
                            paths.append(path);basis.append({'kind':'verified_entity' if entry['status']=='verified' else 'synthetic_entity','mapping':entry,'resolver_version':mapping['resolver_version']})
                            claims.append({'event_id':event_id,'event_version':stored.event_version,'claim_id':claim['claim_id']})
        if basis:hits.append(dict(event_id=event_id,channel='structured',raw_score=float(len(basis)),feature_ids=sorted(set(fids)),event_paths=sorted(set(paths)),claim_refs=claims,basis=basis))
    hits.sort(key=lambda h:(-h['raw_score'],h['event_id']))
    return [dict(h,rank=i) for i,h in enumerate(hits[:top_k],1)]
