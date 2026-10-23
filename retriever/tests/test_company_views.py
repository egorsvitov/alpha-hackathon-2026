import copy
import csv
from pathlib import Path
import tempfile
import unittest
from ranking.profiles import CompanyRepository, refresh_profile_version
from retriever.company_views import build_company_views, build_universal_view
from retriever.representation_contracts import ViewPolicy, InvalidProfileReference

FIXTURE = Path('ranking/tests/fixtures/profiles/companies.csv')

class CompanyViewTests(unittest.TestCase):
    def setUp(self):
        self.profile = CompanyRepository(FIXTURE, is_synthetic=True).get('DEMO_LOGISTICS')
        self.policy = ViewPolicy()

    def change(self, profile, path, value, quality='observed'):
        parts=path.strip('/').split('/'); target=profile
        for part in parts[:-1]: target=target[part]
        target[parts[-1]]=value
        profile['features'][profile['company_id']+':'+path].update(value=value, quality=quality)
        return refresh_profile_version(profile)

    def build(self, p=None): return build_company_views(p or self.profile, policy=self.policy)

    def test_views_refs_topics_nonmutation(self):
        original=copy.deepcopy(self.profile); result=self.build()
        self.assertEqual(self.profile,original)
        self.assertEqual(set(result.views), {'business','finance','trade_geo'})
        for v in result.views.values():
            for r in v.feature_refs:
                self.assertIn(r.feature_id,self.profile['features'])
                self.assertEqual(r.profile_version,self.profile['profile_version'])
            self.assertNotIn(self.profile['company_id'],v.text)
        self.assertTrue(all(t['status']=='conditional' for t in result.views['finance'].search_topics))
        universal=build_universal_view(result,policy=self.policy)
        self.assertEqual(universal.text,' '.join(result.views[k].text for k in ('business','finance','trade_geo')))

    def test_products_only_true_and_business_independent(self):
        first=self.build(); changed=copy.deepcopy(self.profile)
        for key in changed['products']: self.change(changed,'/products/'+key,None if key=='loan' else False)
        second=self.build(changed)
        self.assertEqual(first.views['business'].content_hash,second.views['business'].content_hash)
        self.assertEqual(second.omitted['finance'],'no_positive_products')

    def test_activity_without_coarse_industry(self):
        p=copy.deepcopy(self.profile)
        self.change(p,'/industry',None,'unknown');self.change(p,'/okved/code','86.10')
        self.change(p,'/okved/name','Деятельность больничных организаций')
        self.assertIn('Деятельность больничных организаций',self.build(p).views['business'].text)
        self.change(p,'/okved/name',None,'unknown')
        self.assertIn('business',self.build(p).omitted)

    def test_trade_cases(self):
        p=copy.deepcopy(self.profile)
        self.change(p,'/region',None,'unknown'); self.change(p,'/countries',[],'unknown')
        self.change(p,'/fts_present',True)
        self.assertEqual(self.build(p).views['trade_geo'].text,'В записи отмечено участие во внешней торговле.')
        self.change(p,'/fts_present',None,'unknown'); self.change(p,'/countries',['CN'])
        self.assertIn('участие требует уточнения',self.build(p).views['trade_geo'].text)
        self.change(p,'/fts_present',False,'conflicting');self.change(p,'/countries',['CN'],'conflicting')
        self.assertIn('trade_geo',self.build(p).omitted)
        self.change(p,'/region','Москва'); self.change(p,'/countries',['CN','UNKNOWN:Atlantis'],'unknown')
        v=self.build(p).views['trade_geo'];self.assertEqual(v.text,'Регион регистрации: Москва.')
        self.assertIn('/countries',v.unknowns)

    def test_semantic_hash_ignores_source_and_identity(self):
        first=self.build(); p=copy.deepcopy(self.profile)
        p['source_snapshot']['source_sha256']='another';p['profile_version']='changed'
        for f in p['features'].values(): f['origin']['data_record_numbers']=[99]
        second=self.build(p)
        for k in first.views:
            self.assertEqual(first.views[k].text_hash,second.views[k].text_hash)
            self.assertEqual(first.views[k].content_hash,second.views[k].content_hash)
            self.assertNotEqual(first.views[k].source_provenance,second.views[k].source_provenance)

    def test_invalid_reference_fails(self):
        self.profile['features'].pop(self.profile['company_id']+':/okved/name')
        with self.assertRaises(InvalidProfileReference): self.build()
