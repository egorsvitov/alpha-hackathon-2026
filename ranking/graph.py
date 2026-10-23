"""Conservative payment aggregation; disk-backed pair grouping bounds scan memory."""
from __future__ import annotations
import copy
from collections import OrderedDict
import json
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path
from .profiles import (ROOT, ProfileError, canonical_hash, csv_rows, decimal_string,
                       file_hash, parse_decimal, refresh_profile_version, source_locator, resolve_data_path)

POLICY = 'exclude-all-repeated-directed-pairs-v1'
_CACHE = OrderedDict()

def _persist_artifacts(artifacts, config):
    state_dir = resolve_data_path(config.get('state_dir',ROOT/'data/processed/personalization'))
    artifact_dir = state_dir/'artifacts'
    artifact_dir.mkdir(parents=True,exist_ok=True)
    for artifact_id,artifact in artifacts.items():
        (artifact_dir/(artifact_id.split(':')[1]+'.json')).write_text(json.dumps(artifact,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n',encoding='utf-8')

def attach_payment_graph(profiles, known_company_ids, payments_path, *, config=None):
    config = config or {}
    path = resolve_data_path(payments_path)
    if not path.is_file():
        raise ProfileError('payments_csv_missing', 'Provide an existing --payments-csv path')
    wanted = {p['company_id'] for p in profiles}
    if len(wanted) != len(profiles):
        raise ProfileError('duplicate_profile_id', 'Selected profiles must have unique IDs')
    source_hash = file_hash(path)
    policy = {'policy':POLICY,'count_parser':'positive-dot-decimal-integer','config':{k:v for k,v in config.items() if k != 'state_dir'}}
    cache_key = canonical_hash({'source_sha256':source_hash,'source_path':source_locator(path),'policy':policy,'wanted':sorted(wanted),'known':sorted(known_company_ids)})
    if cache_key in _CACHE:
        graphs, cached_stats = copy.deepcopy(_CACHE[cache_key])
        _CACHE.move_to_end(cache_key)
        result = copy.deepcopy(profiles)
        for profile in result:
            graph, graph_features = graphs[profile['company_id']]
            profile['graph'] = graph
            profile['features'] = {fid:f for fid,f in profile['features'].items() if not f['path'].startswith('/graph/')}
            profile['features'].update(graph_features)
            refresh_profile_version(profile)
        _persist_artifacts(cached_stats['artifacts'], config)
        return result, cached_stats
    stats = {'rows_read':0,'selected_rows':0,'self_loops_excluded':0,'invalid_selected_rows':0,'ambiguous_pair_rows_excluded':0,'unassignable_records':0}
    # Database is ephemeral; the small reproducible aggregation artifact survives separately.
    with tempfile.TemporaryDirectory(prefix='payment-graph-') as tmp:
        db = sqlite3.connect(str(Path(tmp)/'pairs.sqlite'))
        db.execute('PRAGMA cache_size=-2048')
        db.execute('CREATE TABLE edges (payer TEXT, beneficiary TEXT, record INTEGER, amount TEXT, count TEXT, valid INTEGER)')
        for record,row in csv_rows(path, {'PAYER','BENEFICIARY','PAY_AMT','PAY_CNT'}):
            stats['rows_read'] += 1
            payer, beneficiary = row['PAYER'].strip(), row['BENEFICIARY'].strip()
            if not payer or not beneficiary:
                stats['unassignable_records'] += 1
            if payer not in wanted and beneficiary not in wanted:
                continue
            stats['selected_rows'] += 1
            if payer == beneficiary and payer:
                stats['self_loops_excluded'] += 1
                continue
            amount, count = parse_decimal(row['PAY_AMT']), parse_decimal(row['PAY_CNT'])
            count_valid = count is not None and count > 0 and count == count.to_integral_value()
            valid = bool(payer and beneficiary and amount is not None and amount > 0 and count_valid)
            if not valid:
                stats['invalid_selected_rows'] += 1
            db.execute('INSERT INTO edges VALUES (?,?,?,?,?,?)',(payer,beneficiary,record,decimal_string(amount) if amount is not None and amount > 0 else None,str(int(count)) if count_valid else None,int(valid)))
        db.execute('CREATE INDEX pairs ON edges(payer,beneficiary)')
        db.execute('CREATE TABLE multiplicity AS SELECT payer,beneficiary,COUNT(*) AS n FROM edges GROUP BY payer,beneficiary')
        db.execute('CREATE UNIQUE INDEX pair_counts ON multiplicity(payer,beneficiary)')
        stats['ambiguous_pair_rows_excluded'] = db.execute('SELECT COALESCE(SUM(n),0) FROM multiplicity WHERE n>1').fetchone()[0]
        if file_hash(path) != source_hash:
            raise ProfileError('source_changed','Payment CSV changed while loading')
        artifacts, result = {}, copy.deepcopy(profiles)
        for profile in result:
            uid = profile['company_id']
            neighbors, coverage, aggregations = [], {}, {}
            for direction,column in [('incoming','beneficiary'),('outgoing','payer')]:
                c = {'input_records':0,'retained_records':0,'excluded_ambiguous_records':0,'excluded_invalid_records':0}
                retained_amount, positive_amount = Decimal(0), Decimal(0)
                all_amounts_known = True
                retained = []
                query = f'SELECT e.payer,e.beneficiary,e.record,e.amount,e.count,e.valid,m.n FROM edges e JOIN multiplicity m ON e.payer=m.payer AND e.beneficiary=m.beneficiary WHERE e.{column}=? ORDER BY e.payer,e.beneficiary,e.record'
                for payer,beneficiary,record,amount,count,valid,n in db.execute(query,(uid,)):
                    c['input_records'] += 1
                    if amount is None:
                        all_amounts_known = False
                    else:
                        positive_amount += Decimal(amount)
                    if n > 1:
                        c['excluded_ambiguous_records'] += 1
                    elif not valid:
                        c['excluded_invalid_records'] += 1
                    else:
                        c['retained_records'] += 1
                        retained_amount += Decimal(amount)
                        retained.append((payer if direction == 'incoming' else beneficiary,record,amount,count))
                has_losses = c['excluded_ambiguous_records'] + c['excluded_invalid_records'] > 0 or stats['unassignable_records'] > 0
                c.update(retained_record_fraction=round(c['retained_records']/c['input_records'],12) if c['input_records'] else None,
                         retained_amount_raw=decimal_string(retained_amount),valid_positive_input_amount_raw=decimal_string(positive_amount),
                         retained_positive_amount_fraction=round(float(retained_amount/positive_amount),12) if all_amounts_known and positive_amount > 0 else None,
                         share_quality=('limited' if has_losses else 'clean_observed') if retained else 'unavailable')
                coverage[direction] = c
                aggregations[direction] = {'retained_data_record_numbers':[edge[1] for edge in retained],'denominator_raw':decimal_string(retained_amount),'coverage':c}
                for other,record,amount,count in retained:
                    neighbors.append({'company_id':other,'direction':direction,'observed_amount_raw':amount,'payment_count':int(count),'observed_share':round(float(Decimal(amount)/retained_amount),12),'profile_known':other in known_company_ids,'feature_id':''})
            artifact = {'schema_version':'0.1','company_id':uid,'source_path':source_locator(path),'source_sha256':source_hash,'policy':policy,'known_company_ids_sha256':canonical_hash(sorted(known_company_ids)),'directions':aggregations}
            artifact_hash = canonical_hash(artifact)
            artifact_id = 'payment-aggregation:'+artifact_hash
            artifacts[artifact_id] = artifact
            graph_version = canonical_hash({'artifact_sha256':artifact_hash})
            neighbors.sort(key=lambda n:(n['direction'],n['company_id']))
            unique = {n['company_id'] for n in neighbors}
            graph = {'status':'loaded','graph_version':graph_version,'period':None,'currency':None,'neighbors':neighbors,'profile_coverage_by_nodes':round(len(unique & known_company_ids)/len(unique),12) if unique else None,'graph_coverage':coverage,'limitations':['undated_sample_not_total_company_turnover','currency_and_period_unknown','payer_payee_not_proven_supplier_customer_roles','all_repeated_directed_pairs_excluded']}
            if stats['unassignable_records']:
                graph['limitations'].append('unassignable_payment_records_limit_coverage')
            # Replace graph features rather than appending suffixes or stale feature references.
            profile['features'] = {fid:f for fid,f in profile['features'].items() if not f['path'].startswith('/graph/')}
            profile['graph'] = graph
            def add(path,value):
                fid = uid+':'+path
                profile['features'][fid] = {'feature_id':fid,'path':path,'value':copy.deepcopy(value),'quality':'derived' if value is not None else 'unknown','origin':{'kind':'derived','input_feature_ids':[uid+':/company_id'],'rule_version':POLICY+':'+canonical_hash(policy),'artifact_id':artifact_id,'artifact_sha256':artifact_hash}}
                return fid
            for index,neighbor in enumerate(neighbors):
                neighbor['feature_id'] = uid+f':/graph/neighbors/{index}'
                add(f'/graph/neighbors/{index}',neighbor)
            for direction in coverage:
                add('/graph/graph_coverage/'+direction,coverage[direction])
            add('/graph/profile_coverage_by_nodes',graph['profile_coverage_by_nodes'])
            refresh_profile_version(profile)
        db.close()
    final_stats = {**stats,'artifacts':artifacts,'source_sha256':source_hash}
    graphs = {p['company_id']:(p['graph'],{fid:f for fid,f in p['features'].items() if f['path'].startswith('/graph/')}) for p in result}
    _CACHE[cache_key] = copy.deepcopy((graphs,final_stats))
    while len(_CACHE) > 2:
        _CACHE.popitem(last=False)
    _persist_artifacts(artifacts, config)
    return result, final_stats
