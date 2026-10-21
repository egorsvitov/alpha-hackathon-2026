"""Deterministic heuristic ranking of pinned B6 snapshots; no model/API calls."""
from __future__ import annotations
import copy
import json
import re
from datetime import datetime,timezone,timedelta
from pathlib import Path
from .contracts import StoredEvent
from .signals import canonical_hash,load_mappings,prepare_signals,mapping_candidates,disputed,feature
from .entity_mapping import validate_entity_mapping,resolve


def load_rules(path=None):
    rules=json.loads(Path(path or Path(__file__).parent/'config/rules.json').read_text())
    validate_rules(rules)
    return rules

def validate_rules(rules):
    from .signals import GROUPS
    if set(rules['group_caps'])!=GROUPS: raise ValueError('all group caps required')
    if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not 0<=v<float('inf') for v in rules['group_caps'].values()): raise ValueError('invalid cap')
    if rules['half_life_days']<=0 or rules['lookback_days']<0 or rules['fallback_max_days']<rules['lookback_days']: raise ValueError('invalid temporal policy')
    for weights in rules['roles'].values():
        if set(weights)-GROUPS or any(not .8<=x<=1.2 for x in weights.values()): raise ValueError('invalid role multiplier')

def _dt(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('timezone-aware timestamp required')
    return d

def _day(value):
    return datetime.fromisoformat(value).date()

def temporal_policy(snapshot,signals,as_of,rules):
    e=snapshot.bundle['event'];codes=[];policy='decay'
    if snapshot.received_at>as_of: return 'excluded',['receipt_in_future'],None,policy
    if e.get('updated_at') and _dt(e['updated_at'])>as_of: return 'excluded',['producer_update_in_future'],None,policy
    if any(source.get('published_at') and _dt(source['published_at'])>as_of for source in snapshot.bundle.get('sources',[])): return 'excluded',['source_publication_in_future'],None,policy
    if e['availability']=='withdrawn': return 'excluded',['event_withdrawn'],None,policy
    if e['quality']=='insufficient': return 'excluded',['insufficient_evidence'],None,policy
    pub=e.get('first_published_at')
    if not pub: return 'deferred',['publication_date_unknown'],None,policy
    age=(as_of-_dt(pub)).total_seconds()/86400
    if age<0: return 'excluded',['publication_in_future'],None,policy
    if age<=rules['lookback_days']: return 'eligible',codes,age,policy
    safe=[]
    for c in signals['claims']:
        cid=c['claim_id'];interval=c.get('time',{}).get('effective_at')
        if c['polarity']!='affirmed' or c['modality']!='asserted' or not interval or not interval.get('start'): continue
        if any(disputed(signals,cid,p) for p in ['/stage','/time/effective_at','/time/effective_at/start','/modality','/polarity','/object','/action']): continue
        safe.append((c,_day(interval['start']),_day(interval['end']) if interval.get('end') else None))
    if age<=rules['fallback_max_days']:
        for c,start,end in safe:
            if c['stage'] in {'approved','effective'} and as_of.date()<=start<=(as_of+timedelta(days=rules['upcoming_days'])).date() and end is not None:
                return 'eligible',['upcoming_effective_change'],age,'upcoming_floor'
        if e['event_type'] in {'sanction_or_trade_restriction','regulation'}:
            for c,start,end in safe:
                # A conflicting/repealed/negated object prevents durable status inference.
                siblings=[x for x in signals['claims'] if x.get('object')==c.get('object')]
                unsafe=any(x['polarity']!='affirmed' or x.get('stage')=='repealed' or x.get('action') in {'repeal','cancel','lift','remove','revoke'} or any(disputed(signals,x['claim_id'],p) for p in ['/stage','/time/effective_at','/time/effective_at/start']) for x in siblings)
                if c['stage']=='effective' and start<=as_of.date() and not unsafe:
                    return 'eligible',['durable_recheck'],age,'durable_recheck_floor'
    return 'deferred',['durable_status_requires_refresh' if e['event_type']=='sanction_or_trade_restriction' else 'outside_lookback'],age,policy

def _entity_awards(snapshot,signals,profile,mapping,as_of):
    out=[];diag=[];b=snapshot.bundle
    synthetic=bool(b.get('sources')) and all(s.get('is_synthetic') is True for s in b['sources'])
    for i,c in enumerate(signals['claims']):
        for j,p in enumerate(c.get('participants',[])):
            if p.get('role') not in {'target','subject','affected_company'}: continue
            if disputed(signals,c['claim_id'],f'/participants/{j}'): continue
            for entry in resolve(mapping,p['entity_id'],as_of=as_of,source_mode=snapshot.source_mode,is_synthetic=synthetic):
                limits=['entity_mapping:'+mapping['resolver_version']]
                if entry['status']=='synthetic': limits.append('synthetic_mapping_demo_only')
                paths=[f'/claims/{i}/participants/{j}']
                if entry['company_id']==profile['company_id']:
                    f=feature(profile,'/company_id')
                    if f: out.append({'group':'entity','points':65,'rule_id':'verified_entity','claim_id':c['claim_id'],'feature_ids':[f['feature_id']],'event_paths':paths,'basis':'observed_match','limitations':limits,'text':'Подтверждённое сопоставление объекта новости с компанией; '+c['modality']+'/'+c['polarity']})
                    continue
                graph=profile['graph']
                if graph['status']!='loaded': diag.append('payment_graph_'+graph['status']);continue
                for n in graph['neighbors']:
                    if n['company_id']!=entry['company_id']: continue
                    q=graph['graph_coverage'][n['direction']]['share_quality']
                    if q=='unavailable':diag.append('payment_share_unavailable');continue
                    points=5 if q=='limited' else 20+35*n['observed_share']
                    out.append({'group':'counterparty','points':points,'rule_id':'payment_share_limited' if q=='limited' else 'payment_observed_link','claim_id':c['claim_id'],'feature_ids':[n['feature_id']],'event_paths':paths,'basis':'observed_match','limitations':limits+['payment_subset_not_economic_dependency', 'payment_share_'+q],'text':f"Сохранённая связь платежей, направление {n['direction']}; доля очищенного поднабора не доказывает экономическую зависимость."})
    return out,diag

def candidate_report(snapshot,profile,*,as_of,rules=None,entity_mapping=None):
    rules=rules or load_rules();s=prepare_signals(snapshot)
    eligibility,codes,age,age_policy=temporal_policy(snapshot,s,as_of,rules)
    awards,diagnostics=mapping_candidates(s,profile,rules.get('mappings') or load_mappings())
    entity,limits=_entity_awards(snapshot,s,profile,entity_mapping,as_of);awards+=entity
    # Limited lexical fallback: multiple substantive exact words, never headline words.
    name=(profile.get('okved') or {}).get('name') or ''
    stop={'компания','компании','рынок','рынка','россия','россии','услуги','деятельность','производство'}
    words=lambda text:{w for w in re.findall(r'[^\W\d_]{4,}',text.casefold()) if w not in stop}
    f=feature(profile,'/okved/name')
    for i,c in enumerate(s['claims']):
        if f and f['quality'] not in {'unknown','conflicting'} and len(words(name)&words(c['text']))>=2:
            awards.append({'group':'lexical','points':10,'rule_id':'lexical_overlap','claim_id':c['claim_id'],'feature_ids':[f['feature_id']],'event_paths':[f'/claims/{i}/text'],'basis':'lexical_match','limitations':['lexical_similarity_not_impact'],'text':'Несколько содержательных совпадений текста утверждения и вида деятельности.'})
    if not awards and eligibility=='eligible': eligibility='deferred';codes.append('no_candidate_signal_unknown_relevance')
    return {'eligibility':eligibility,'reason_codes':codes,'age_days':age,'age_policy':age_policy,'awards':awards,'diagnostics':diagnostics,'limitations':limits}

def rank_events(snapshots:list[StoredEvent],profile:dict,*,role:str,as_of:datetime,rules:dict,entity_mapping=None,top_k:int=5)->dict:
    if as_of.tzinfo is None: raise ValueError('as_of must be timezone-aware')
    validate_rules(rules);validate_entity_mapping(entity_mapping)
    if role not in rules['roles']: raise ValueError('unsupported role')
    if top_k<0: raise ValueError('top_k must be nonnegative')
    # Select latest before eligibility; never resurrect an earlier active version.
    latest={}
    for s in snapshots:
        if s.received_at>as_of or _dt(s.bundle['event']['updated_at'])>as_of: continue
        if s.event_id not in latest or s.event_version>latest[s.event_id].event_version:latest[s.event_id]=s
    snapshots=sorted(latest.values(),key=lambda s:s.event_id)
    mappings=rules.get('mappings') or load_mappings()
    rule_hash=canonical_hash({'rules':rules,'mappings':mappings,'entity_mapping':entity_mapping})
    # Resolve configuration once per run, while preserving the caller's config/hash.
    candidate_rules={**rules,'mappings':mappings}
    limitation=['heuristic_uncalibrated_priority']
    hist=profile.get('source_snapshot',{})
    if hist.get('historical_applicability')!='dated_snapshot':limitation.append('profile_historical_applicability_unknown')
    elif hist.get('data_as_of') and _dt(hist['data_as_of'])!=as_of:limitation.append('profile_snapshot_date_differs_from_as_of')
    result={'schema_version':'0.1','company_id':profile['company_id'],'profile_version':profile['profile_version'],'user_role':role,'as_of':as_of.isoformat(),'input_snapshot_hash':canonical_hash([{k:v for k,v in s.snapshot.to_dict().items() if k!='ingest_id'} for s in snapshots]),'scoring_version':rules['scoring_version'],'rules_sha256':rule_hash,'ranked':[],'feed':[],'deferred':[],'excluded':[],'limitations':limitation}
    topics={}
    for s in snapshots:
        report=candidate_report(s,profile,as_of=as_of,rules=candidate_rules,entity_mapping=entity_mapping)
        snap=s.snapshot.to_dict();limits=sorted(set(limitation+report['limitations']+s.bundle.get('limitations',[])))
        if report['eligibility']!='eligible':
            result[report['eligibility']].append({'snapshot':snap,'reason_codes':report['reason_codes'],'limitations':limits});continue
        winners={}
        for a in sorted(report['awards'],key=lambda a:(-a['points'],a['rule_id'],a['claim_id'])): winners.setdefault(a['group'],a)
        age=2**(-max(0,report['age_days'])/rules['half_life_days'])
        if report['age_policy']!='decay': age=max(age,rules['fallback_age_floor'])
        groups=[];reasons=[]
        for group,a in sorted(winners.items()):
            raw=min(rules['group_caps'][group],a['points']);mult=rules['roles'][role].get(group,1);weighted=raw*mult*age
            groups.append({'group':group,'raw_points':raw,'role_multiplier':mult,'age_weight':age,'weighted_points':weighted})
            reasons.append({'reason_id':a['rule_id']+':'+a['claim_id']+':'+group,'group':group,'text':a['text'],'raw_points':raw,'contribution':weighted,'feature_ids':a['feature_ids'],'claim_ids':[a['claim_id']],'event_paths':a['event_paths'],'basis':a['basis']})
        raw_total=sum(g['weighted_points'] for g in groups);score=min(1,raw_total/100)
        for r in reasons:r['contribution']=score*r['contribution']/raw_total if raw_total else 0
        limits=sorted(set(limits+[x for a in report['awards'] for x in a['limitations']]))
        if report['age_policy']=='durable_recheck_floor':limits.append('Ранее сообщалось о вступлении ограничения в силу; проверить актуальность. effective_at.end не является датой прекращения.')
        if report['age_policy']=='upcoming_floor':limits.append('old_announcement_upcoming_effective_window')
        limited=bool(s.bundle.get('conflicts')) or s.bundle['event']['quality']=='partial' or report['age_policy']!='decay' or any(x in limits for x in ['event_geography_unknown','payment_share_limited'])
        rel={'schema_version':'0.1','event_id':s.event_id,'event_version':s.event_version,'company_id':profile['company_id'],'profile_version':profile['profile_version'],'user_role':role,'as_of':as_of.isoformat(),'eligibility':'eligible','reason_codes':report['reason_codes'],'score':score,'raw_total':raw_total,'scoring_diagnostics':{'groups':groups,'age_policy':report['age_policy'],'score_saturated':raw_total>=100},'level':'high' if score>=rules['high_threshold'] else 'medium' if score>=rules['medium_threshold'] else 'low','reasons':reasons,'candidate_channels':sorted(winners),'feature_ids':sorted({f for r in reasons for f in r['feature_ids']}),'claim_ids':sorted({c for r in reasons for c in r['claim_ids']}),'evidence_status':'limited' if limited else 'usable','limitations':limits,'scoring_version':rules['scoring_version'],'rules_sha256':rule_hash}
        result['ranked'].append({'snapshot':snap,'relevance':rel})
        topics[s.event_id]=sorted(s.bundle['ranking_features'].get('topics',[]))[0] if s.bundle['ranking_features'].get('topics') else '__unknown__'
    result['ranked'].sort(key=lambda x:(-x['relevance']['score'],-x['relevance']['raw_total'],x['snapshot']['event_id']))
    counts={}
    for row in result['ranked']:
        rel=row['relevance'];topic=topics[rel['event_id']]
        if len(result['feed'])>=top_k:break
        if rel['score']<rules['min_feed_score']:continue
        if counts.get(topic,0)>=rules['max_per_topic'] and not (rules['high_priority_override'] and rel['level']=='high'):continue
        result['feed'].append(copy.deepcopy(row));counts[topic]=counts.get(topic,0)+1
    return result
