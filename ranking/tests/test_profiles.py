import copy
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from ranking.profiles import CompanyRepository, DEFAULT_CONFIG, ProfileError, file_hash, parse_flag
from ranking.contracts import resolve_pointer, validate_contract

FIXTURE = Path(__file__).parent/'fixtures/profiles/companies.csv'

class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with FIXTURE.open(newline='',encoding='utf-8') as f:
            self.rows = list(csv.DictReader(f,delimiter=';'))

    def repo(self, rows=None, **kwargs):
        path = self.root/'companies.csv'
        with path.open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=list(self.rows[0]),delimiter=';')
            writer.writeheader();writer.writerows(rows if rows is not None else self.rows)
        return CompanyRepository(path,is_synthetic=True,**kwargs)

    def test_p01_tristate(self):
        for v in ['1','1.0',True]:self.assertIs(parse_flag(v),True)
        for v in ['0','0.0',False]:self.assertIs(parse_flag(v),False)
        for v in ['',None,'bad','2','NaN']:self.assertIsNone(parse_flag(v))
        row=copy.deepcopy(self.rows[0]);row['CL_PRODUCT_LOA_MARK']='bad'
        p=self.repo([row]).get(row['SYNTHETIC_U'])
        self.assertIsNone(p['products']['loan']);self.assertIn('unexpected_flag:CL_PRODUCT_LOA_MARK',p['limitations'])

    def test_p02_duplicates_and_conflicts(self):
        row=self.rows[0]
        r=self.repo([row,row]);p=r.get(row['SYNTHETIC_U'])
        self.assertIn('identical_duplicate_records_collapsed',p['limitations'])
        self.assertEqual(p['features'][row['SYNTHETIC_U']+':/company_id']['origin']['data_record_numbers'],[1,2])
        altered=dict(row,CITY='Другой город');r=self.repo([row,altered])
        with self.assertRaises(ProfileError) as caught:r.get(row['SYNTHETIC_U'])
        self.assertEqual(caught.exception.code,'profile_conflict')

    def test_p03_categories_decimal_unknown_units(self):
        p=self.repo().get('DEMO_LOGISTICS')
        self.assertEqual(p['revenue']['raw'],'123456.78');self.assertIsNone(p['revenue']['unit'])
        self.assertEqual(p['age_band'],'3–5');self.assertEqual(p['employees_band'],'16–100')
        for amount in ['NaN','Infinity','-1','1,234','1 234','']:
            row=dict(self.rows[0],REVENUE_COMPANY=amount)
            self.assertIsNone(self.repo([row]).get(row['SYNTHETIC_U'])['revenue']['raw'])

    def test_p04_new_id_unknowns_and_conflict(self):
        row=dict(self.rows[0],SYNTHETIC_U='NEW/FICTIONAL',MAINOKVED_CODE='bad',FTS_COUNTRY_NAME='Atlantis, CN, CHINA',FTS_FLAG='0')
        p=self.repo([row]).get('NEW/FICTIONAL')
        self.assertIsNone(p['industry']);self.assertEqual(p['countries'],['CN','UNKNOWN:Atlantis'])
        self.assertEqual(p['features']['NEW/FICTIONAL:/countries']['quality'],'conflicting')
        self.assertEqual(p['features']['NEW/FICTIONAL:/fts_present']['quality'],'conflicting')

    def test_p05_schema_and_every_pointer_origin(self):
        repo=self.repo()
        for uid in sorted(repo.company_ids):
            p=repo.get(uid)
            self.assertEqual(validate_contract(p,'company-profile'),[])
            for fid,f in p['features'].items():
                self.assertEqual(fid,f['feature_id']);self.assertEqual(resolve_pointer(p,f['path']),f['value'])
                for ref in f['origin'].get('input_feature_ids',[]):self.assertIn(ref,p['features']);self.assertNotEqual(ref,fid)
                if f['origin']['kind']=='csv':self.assertEqual(f['origin']['source_sha256'],repo.sha256)

    def test_p08_versions_content_config_and_import(self):
        first=self.repo(imported_at='2026-01-01T00:00:00Z').get('DEMO_IT')
        later=self.repo(imported_at='2026-10-01T00:00:00Z').get('DEMO_IT')
        self.assertEqual(first['profile_version'],later['profile_version'])
        config=json.loads(DEFAULT_CONFIG.read_text());config['country_aliases']['ATLANTIS']='ZZ'
        path=self.root/'mapping.json';path.write_text(json.dumps(config))
        changed=self.repo(normalization_config=path).get('DEMO_IT')
        self.assertNotEqual(first['profile_version'],changed['profile_version'])
        rows=copy.deepcopy(self.rows);rows[1]['CITY']='Изменение'
        self.assertNotEqual(first['profile_version'],self.repo(rows).get('DEMO_IT')['profile_version'])

    def test_p10_manifest_dates(self):
        repo=self.repo();p=repo.get('DEMO_IT')
        self.assertIsNone(p['source_snapshot']['data_as_of']);self.assertIsNone(p['source_snapshot']['exported_at'])
        self.assertEqual(p['source_snapshot']['historical_applicability'],'unknown')
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps({'source_sha256':repo.sha256,'data_as_of':'2026-10-01T00:00:00Z','exported_at':'2026-10-02T00:00:00Z'}))
        dated=CompanyRepository(repo.path,source_manifest=manifest,is_synthetic=True).get('DEMO_IT')
        self.assertEqual(dated['source_snapshot']['historical_applicability'],'dated_snapshot')
        self.assertNotEqual(p['profile_version'],dated['profile_version'])
        self.assertEqual(dated['features']['DEMO_IT:/source_snapshot/data_as_of']['origin']['kind'],'manifest')

    def test_csv_validation_and_record_not_physical_line(self):
        for text in ['SYNTHETIC_U;SYNTHETIC_U\nA;B\n','x\n1\n']:
            path=self.root/'bad.csv';path.write_text(text)
            with self.assertRaises(ProfileError):CompanyRepository(path)
        for row in [dict(self.rows[0],SYNTHETIC_U='')]:
            with self.assertRaises(ProfileError):self.repo([row])
        row=dict(self.rows[0],MAINOKVED_NAME='Quoted\nnewline')
        p=self.repo([row,self.rows[1]]).get('DEMO_IT')
        self.assertEqual(p['features']['DEMO_IT:/company_id']['origin']['data_record_numbers'],[2])
        path=self.root/'companies.csv';text=path.read_text();path.write_text(text+'extra;cells\n')
        with self.assertRaises(ProfileError):CompanyRepository(path)
        with self.assertRaises(ProfileError):self.repo().get('absent')

    def test_cli_synthetic_no_raw(self):
        output=self.root/'profile.json'
        cmd=[sys.executable,'-m','ranking','profile','--company-id','DEMO_IT','--companies-csv',str(FIXTURE),'--synthetic','--output',str(output)]
        done=subprocess.run(cmd,capture_output=True,text=True)
        self.assertEqual(done.returncode,0,done.stderr)
        self.assertEqual(validate_contract(json.loads(output.read_text()),'company-profile'),[])
        failed=subprocess.run(cmd+['--company-id','missing'],capture_output=True,text=True)
        self.assertEqual(failed.returncode,2);self.assertIn('company_not_found',failed.stderr)
