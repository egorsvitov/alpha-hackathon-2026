import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from llm.agent.run_store import RunStore, _decode

class RunStoreTests(unittest.TestCase):
    def test_restart_pins_full_context_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'runs.sqlite'
            context = {'profile':{'company_id':'C'},'snapshot':{'version':1},'registry':{'s':{'quote':'original'}},'config':{'mode':'fixture'}}
            with RunStore(path) as store:
                store.create_run('../../outside',context)
                aid = store.save_artifact('../../outside','draft',{'text':'one'})
                store.record_step('../../outside',{'state':'draft_generated','input_artifact_ids':[],'output_artifact_ids':[aid]})
                store.finish_run('../../outside',{'status':'limited'})
            context['profile']['company_id'] = 'MUTATED'
            with RunStore(path) as store:
                saved = store.get_run('../../outside')
                self.assertEqual(saved['context']['profile']['company_id'],'C')
                self.assertEqual(saved['artifacts'][0]['payload'],{'text':'one'})
                self.assertEqual(saved['steps'][0]['output_artifact_ids'],[aid])
                self.assertEqual(saved['result']['status'],'limited')
                for action in (lambda:store.record_step('../../outside',{}),lambda:store.save_artifact('../../outside','x',{}),lambda:store.finish_run('../../outside',{})):
                    with self.assertRaisesRegex(ValueError,'run_sealed'): action()
                before = store.get_run('../../outside')
                store.create_run('child',saved['context'],parent_run_id='../../outside')
                store.finish_run('child',{'answer':'new'})
                self.assertEqual(store.get_run('../../outside'),before)
                self.assertEqual(store.get_run('child')['parent_run_id'],'../../outside')
            self.assertFalse((Path(tmp).parent/'outside').exists())

    def test_duplicate_run_and_nonfinite_are_rejected_atomically(self):
        with RunStore(':memory:') as store:
            store.create_run('r',{})
            with self.assertRaises(sqlite3.IntegrityError): store.create_run('r',{'bad':True})
            with self.assertRaises(ValueError): store.save_artifact('r','bad',{'x':math.nan})
            self.assertEqual(store.get_run('r')['artifacts'],[])
            with self.assertRaises(KeyError): store.create_run('child',{},'missing')
            with self.assertRaises(KeyError): store.get_run('missing')
        for raw in ('{"x":1,"x":2}','{"x":NaN}'):
            with self.assertRaises(ValueError): _decode(raw)

    def test_prior_history_scope_requires_completed_observation(self):
        with RunStore(':memory:') as store:
            context={'profile':{'company_id':'C'},'snapshot':{'event_id':'E'},'role':'cfo'}
            scope=dict(company_id='C',event_id='E',role='cfo')
            self.assertFalse(store.has_prior_run(**scope))
            store.create_run('r',context)
            self.assertFalse(store.has_prior_run(**scope))
            store.finish_run('r',{})
            self.assertTrue(store.has_prior_run(**scope))
            self.assertFalse(store.has_prior_run(**{**scope,'company_id':'ALIEN'}))
            self.assertFalse(store.has_prior_run(**{**scope,'role':'ceo'}))

    def test_corruption_detected(self):
        with RunStore(':memory:') as store:
            store.create_run('r',{})
            aid=store.save_artifact('r','x',{'value':1})
            store.connection.execute('UPDATE agent_artifacts SET payload=? WHERE artifact_id=?',('{"value":2}',aid))
            with self.assertRaisesRegex(ValueError,'artifact_hash_mismatch'): store.get_run('r')

if __name__ == '__main__': unittest.main()
