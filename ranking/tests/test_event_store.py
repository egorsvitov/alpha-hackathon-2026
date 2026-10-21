import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from ranking.contracts import PROJECT_ROOT, FixtureClock, EventNotFound, EventVersionCollision, BundleValidationError
from ranking.event_store import EventStore, ingest_bundles

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'events.sqlite3'
        self.bundle=json.loads((PROJECT_ROOT/'docs/b6-examples/normal.json').read_text())
        self.t='2026-10-20T08:00:00Z';self.later='2026-10-21T08:00:00Z'
    def ingest(self,store,bundles,time=None):
        return ingest_bundles(bundles,store=store,clock=FixtureClock(time or self.t),source_mode='fixture')
    def version(self,n,status='active'):
        b=copy.deepcopy(self.bundle);b['event'].update(event_version=n,availability=status);return b
    def test_idempotence_collision_persistence(self):
        with EventStore(self.path) as store:
            first=self.ingest(store,[self.bundle])[0]
            same=self.ingest(store,[self.bundle],self.later)[0]
            self.assertEqual(first.snapshot,same.snapshot);self.assertEqual(same.status,'unchanged')
            b=copy.deepcopy(self.bundle);b['event']['title']='changed'
            with self.assertRaises(EventVersionCollision):self.ingest(store,[b])
        with EventStore(self.path) as store:
            self.assertEqual(store.get_latest(self.bundle['event']['event_id'],self.later).snapshot,first.snapshot)
    def test_versions_withdrawal_cutoffs(self):
        with EventStore(self.path) as store:
            self.ingest(store,[self.version(2),self.version(1)])
            event_id=self.bundle['event']['event_id']
            self.assertEqual(store.get_latest(event_id,self.t).event_version,2)
            self.ingest(store,[self.version(3,'withdrawn')],self.later)
            self.assertEqual(store.get_latest(event_id,self.t).event_version,2)
            self.assertEqual(store.list_latest(self.later)[0].bundle['event']['availability'],'withdrawn')
            self.ingest(store,[self.version(4)],self.later)
            self.assertEqual(store.get_latest(event_id,self.later).event_version,4)
            for identifier,time in [('missing',self.t),(event_id,'2026-10-19T00:00:00Z')]:
                with self.assertRaises(EventNotFound):store.get_latest(identifier,time)
    def test_numeric_and_producer_cutoff(self):
        with EventStore(self.path) as store:
            self.ingest(store,[self.version(9),self.version(10),self.version(2)])
            self.assertEqual(store.list_latest(self.t)[0].event_version,10)
            future=self.version(11);future['event']['updated_at']=self.later
            self.ingest(store,[future]);self.assertEqual(store.list_latest(self.t)[0].event_version,10)
    def test_atomic_validation_and_collision(self):
        with EventStore(self.path) as store:
            bad=self.version(2);bad['evidence'][0]['end']=999
            with self.assertRaises(BundleValidationError):self.ingest(store,[self.bundle,bad])
            self.assertEqual(store.list_latest(self.later),[])
            self.ingest(store,[self.bundle])
            bad=copy.deepcopy(self.bundle);bad['event']['title']='collision'
            with self.assertRaises(EventVersionCollision):self.ingest(store,[self.version(2),bad])
            self.assertEqual(store.list_latest(self.later)[0].event_version,1)
    def test_one_clock_call_and_warning_persistence(self):
        class Clock:
            calls=0
            def now(inner):inner.calls+=1;return FixtureClock(self.t).now()
        clock=Clock();b=self.bundle;b['sources'][0]['content_kind']='original'
        with EventStore(self.path) as store:
            results=ingest_bundles([b,self.version(2)],store=store,clock=clock,source_mode='fixture')
            self.assertEqual(clock.calls,1);self.assertEqual(results[0].snapshot.received_at,results[1].snapshot.received_at)
            self.assertIn('source_flags_ambiguous',{i.code for i in store.list_latest(self.later)[0].validation_issues})
            with self.assertRaises(ValueError):ingest_bundles([b],store=store,clock=FixtureClock(self.t),source_mode='live')
    def test_concurrent_put(self):
        with EventStore(self.path):pass
        def insert(_):
            with EventStore(self.path) as store:return self.ingest(store,[self.bundle])[0].status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(insert,range(2))),['inserted','unchanged'])
    def test_replay_deterministic(self):
        with EventStore(self.path) as one, EventStore(Path(self.tmp.name)/'other.sqlite3') as two:
            self.assertEqual(self.ingest(one,[self.bundle])[0].snapshot,self.ingest(two,[self.bundle])[0].snapshot)
