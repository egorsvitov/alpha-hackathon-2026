"""Versioned company snapshots, with explicit unknowns and field provenance."""
from __future__ import annotations
import copy
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / 'ranking/config/normalization.json'
PRODUCT_FIELDS = {'rko':'CL_PRODUCT_RKO_MARK','guarantee':'CL_PRODUCT_GAR_MARK','loan':'CL_PRODUCT_LOA_MARK','leasing':'CL_PRODUCT_LEA_MARK','deposit':'CL_PRODUCT_DEP_MARK','payroll':'CL_PRODUCT_ZPP_MARK','factoring':'FACTORING_MARK'}
CATEGORY_FIELDS = {'region':'REGION','city':'CITY','age_band':'COMPANY_AGE_YEARS_GRADATION','employees_band':'AMOUNT_WORKERS_FNS_GRADATION','development_stage':'ECO_MARK_LABEL','group_id':'GROUP_ID','client_type':'CL_TYPE'}

class ProfileError(ValueError):
    def __init__(self, code, message, path=''):
        self.code, self.path = code, path
        super().__init__(f'{code}: {message}')

def resolve_data_path(path):
    path = Path(path)
    return path if path.is_absolute() else ROOT/path

def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()

def source_locator(path):
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return 'external:' + Path(path).name

def parse_flag(value):
    if type(value) is bool:
        return value
    return {'1':True,'1.0':True,'0':False,'0.0':False}.get(str(value if value is not None else '').strip())

def parse_decimal(value):
    """Only unsigned dot-decimal strings; no ambiguous thousands/comma notation."""
    value = str(value or '').strip()
    if not re.fullmatch(r'\+?\d+(?:\.\d+)?', value):
        return None
    try:
        d = Decimal(value)
        return d if d.is_finite() and d >= 0 else None
    except InvalidOperation:
        return None

def decimal_string(value):
    return format(value, 'f')

def load_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProfileError('json_duplicate_key', 'Duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ProfileError('json_nonfinite', 'Nonfinite JSON number')
    return json.loads(resolve_data_path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs, parse_constant=invalid)

def load_config(path=DEFAULT_CONFIG):
    config = load_json(Path(path))
    if not isinstance(config, dict) or not isinstance(config.get('version'), str):
        raise ProfileError('normalization_config_invalid', 'Expected versioned normalization object')
    return config

def industry_for(code, config=None):
    config = config or load_config()
    if not re.fullmatch(r'\d{2}(?:\.\d{1,2}){0,2}', code or ''):
        return None
    return next((name for low, high, name in config['industry_ranges'] if low <= int(code[:2]) <= high), None)

def refresh_profile_version(profile):
    semantic = copy.deepcopy(profile)
    semantic.pop('profile_version', None)
    semantic['source_snapshot'].pop('imported_at', None)
    # This technical timestamp has no business meaning, including its feature value.
    semantic['features'].pop(f"{profile['company_id']}:/source_snapshot/imported_at", None)
    profile['profile_version'] = canonical_hash(semantic)
    return profile

def csv_rows(path, required):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, delimiter=';', strict=True)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or any(not h for h in headers):
            raise ProfileError('csv_duplicate_headers', 'Headers must be unique and nonempty')
        if not set(required) <= set(headers):
            raise ProfileError('csv_missing_headers', 'Missing columns: ' + ', '.join(sorted(set(required)-set(headers))))
        try:
            for index, row in enumerate(reader, 1):
                if None in row or any(v is None for v in row.values()):
                    raise ProfileError('csv_row_width', f'Invalid number of cells at data record {index}')
                yield index, row
        except csv.Error as exc:
            raise ProfileError('csv_parse_error', 'Invalid CSV structure') from exc

class CompanyRepository:
    def __init__(self, csv_path, *, normalization_config=DEFAULT_CONFIG, imported_at=None, source_manifest=None, is_synthetic=False):
        self.path = resolve_data_path(csv_path)
        if not self.path.is_file():
            raise ProfileError('companies_csv_missing', 'Provide an existing --companies-csv path')
        self.config = load_config(normalization_config)
        self.sha256, self.source_path = file_hash(self.path), source_locator(self.path)
        self.imported_at = imported_at or datetime.now(timezone.utc).isoformat()
        from .contracts import parse_timestamp
        parse_timestamp(self.imported_at)
        self.manifest = None
        if source_manifest is not None:
            self.manifest_path = resolve_data_path(source_manifest)
            self.manifest = load_json(self.manifest_path)
            if self.manifest.get('source_sha256') != self.sha256:
                raise ProfileError('manifest_source_mismatch', 'Manifest does not identify this CSV')
            for key in ['data_as_of','exported_at']:
                if self.manifest.get(key) is not None:
                    parse_timestamp(self.manifest[key])
        self.is_synthetic = is_synthetic
        self.rows, self.conflicts, self.duplicates = {}, set(), set()
        self.record_count = 0
        required = {'SYNTHETIC_U','MAINOKVED_CODE','MAINOKVED_NAME',*PRODUCT_FIELDS.values()}
        for record, row in csv_rows(self.path, required):
            self.record_count += 1
            uid = row['SYNTHETIC_U'].strip()
            if not uid:
                raise ProfileError('company_id_empty', f'Empty company ID at data record {record}')
            if uid in self.rows:
                self.duplicates.add(uid)
                if self.rows[uid][0] != row:
                    self.conflicts.add(uid)
                self.rows[uid][1].append(record)
            else:
                self.rows[uid] = (row, [record])
        if file_hash(self.path) != self.sha256:
            raise ProfileError('source_changed', 'CSV changed while loading')

    @property
    def company_ids(self):
        return set(self.rows)

    def get(self, company_id):
        if company_id in self.conflicts:
            raise ProfileError('profile_conflict', 'Company has conflicting source records')
        if company_id not in self.rows:
            raise ProfileError('company_not_found', 'Company ID is absent from this source')
        raw, records = self.rows[company_id]
        features, missing, limitations = {}, [], ['revenue_unit_and_period_unconfirmed','products_describe_supplied_bank_record_only','registered_region_not_all_operations','okved_not_cost_structure','group_id_not_proof_of_ownership']
        def feature(path, value, columns=None, quality='observed', origin=None):
            fid = f'{company_id}:{path}'
            if value is None:
                missing.append(path)
                quality = 'unknown'
            features[fid] = {'feature_id':fid,'path':path,'value':copy.deepcopy(value),'quality':quality,'origin':origin or {'kind':'csv','source_path':self.source_path,'source_sha256':self.sha256,'data_record_numbers':records[:],'columns':columns or []}}
            return value
        def derived(path, value, inputs):
            return feature(path, value, quality='derived', origin={'kind':'derived','input_feature_ids':[f'{company_id}:{p}' for p in inputs],'rule_version':self.config['version']+':'+canonical_hash(self.config),'artifact_id':None,'artifact_sha256':None})
        p = {'schema_version':'0.1','company_id':feature('/company_id',company_id,['SYNTHETIC_U']),'is_synthetic':self.is_synthetic,'display_name':f'Компания {company_id}','features':features,'missing_fields':missing,'limitations':limitations,'normalization_version':self.config['version']+':'+canonical_hash(self.config)}
        p['okved'] = {k:feature('/okved/'+k,raw.get(col,'').strip() or None,[col]) for k,col in [('code','MAINOKVED_CODE'),('name','MAINOKVED_NAME')]}
        p['industry'] = derived('/industry',industry_for(p['okved']['code'],self.config),['/okved/code'])
        p['products'] = {}
        for name,col in PRODUCT_FIELDS.items():
            value = parse_flag(raw.get(col))
            p['products'][name] = feature('/products/'+name,value,[col])
            if value is None and raw.get(col,'').strip():
                limitations.append('unexpected_flag:'+col)
        for name,col in CATEGORY_FIELDS.items():
            value = raw.get(col,'').strip() or None
            if name == 'region' and value is not None:
                value = self.config['region_aliases'].get(value.upper(),value)
            p[name] = feature('/'+name,value,[col])
        p['fts_present'] = feature('/fts_present',parse_flag(raw.get('FTS_FLAG')),['FTS_FLAG'])
        if p['fts_present'] is None and raw.get('FTS_FLAG','').strip():
            limitations.append('unexpected_flag:FTS_FLAG')
        countries, unknown = set(), False
        for token in re.split(r'[,|;]',raw.get('FTS_COUNTRY_NAME','')):
            token = token.strip()
            if token:
                country = self.config['country_aliases'].get(token.upper())
                if country is None:
                    unknown = True
                    country = 'UNKNOWN:'+token
                countries.add(country)
        quality = 'unknown' if unknown or not countries else 'observed'
        if p['fts_present'] is False and countries:
            quality = 'conflicting'
            limitations.append('fts_country_flag_conflict')
            features[f'{company_id}:/fts_present']['quality'] = 'conflicting'
        p['countries'] = feature('/countries',sorted(countries),['FTS_COUNTRY_NAME'],quality)
        if unknown:
            limitations.append('unknown_country_tokens_preserved')
        if not countries:
            missing.append('/countries')
        amount = parse_decimal(raw.get('REVENUE_COMPANY'))
        p['revenue'] = {'raw':feature('/revenue/raw',decimal_string(amount) if amount is not None else None,['REVENUE_COMPANY']),'unit':None,'unit_status':'unconfirmed','period':None}
        for key in ['unit','unit_status','period']:
            derived('/revenue/'+key,p['revenue'][key],['/revenue/raw'])
        manifest = self.manifest or {}
        p['source_snapshot'] = {'source_sha256':self.sha256,'imported_at':self.imported_at,'data_as_of':manifest.get('data_as_of'),'exported_at':manifest.get('exported_at'),'historical_applicability':'dated_snapshot' if manifest.get('data_as_of') else 'unknown'}
        for key in ['data_as_of','exported_at']:
            origin = {'kind':'manifest','source_path':source_locator(self.manifest_path),'source_sha256':file_hash(self.manifest_path),'field_path':'/'+key} if self.manifest else None
            feature('/source_snapshot/'+key,p['source_snapshot'][key],['SYNTHETIC_U'],origin=origin)
        feature('/source_snapshot/source_sha256',self.sha256,['SYNTHETIC_U'])
        derived('/source_snapshot/historical_applicability',p['source_snapshot']['historical_applicability'],['/source_snapshot/data_as_of'])
        if not manifest.get('data_as_of'):
            limitations.append('source_business_date_unknown_historical_applicability_unproven')
        if company_id in self.duplicates:
            limitations.append('identical_duplicate_records_collapsed')
        p['graph'] = {'status':'not_loaded','graph_version':None,'period':None,'currency':None,'neighbors':[],'profile_coverage_by_nodes':None,'graph_coverage':None,'limitations':[]}
        derived('/display_name',p['display_name'],['/company_id'])
        derived('/is_synthetic',p['is_synthetic'],['/company_id'])
        p['missing_fields'] = sorted(set(missing))
        return refresh_profile_version(p)
