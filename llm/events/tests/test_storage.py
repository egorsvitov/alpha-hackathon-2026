from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from llm.events.models import Issue
from llm.events.schemas import ROOT
from llm.events.storage import EventStore, StorageError, processing_hash, source_id_for
from llm.events.storage_models import ProcessingSpec
from llm.events.storage_schema import APPLICATION_ID, SCHEMA_VERSION
from llm.events.validation import validate_grounding


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.synthetic = json.loads((ROOT / "llm/events/fixtures/synthetic.json").read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.clock = Clock()
        self.store = EventStore(self.path, clock=self.clock)
        self.article = deepcopy(self.synthetic["articles"][0]["article"])
        self.spec = ProcessingSpec(processor_name="test-extract", processor_version="1",
            configuration={"model_id":"test:no-provider", "prompt_version":"1", "schema_version":"0.2"})

    def ingest(self, articles=None):
        return self.store.import_batch(json.dumps(articles if articles is not None else [self.article], ensure_ascii=False).encode())

    def source(self):
        return self.ingest().items[0].source_id

    def running(self):
        source = self.source()
        self.store.enqueue(source, self.spec)
        return self.store.claim_next(self.spec, "test-worker")

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(StorageError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertTrue(caught.exception.issues[0].path.startswith("/"))

    def count(self, table):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_initial_fifteen_repeat_and_reordered_keys(self):
        articles = [deepcopy(r["article"]) for r in self.synthetic["articles"][:15]]
        first = self.ingest(articles)
        self.assertEqual(first.counts.new, 15)
        self.assertEqual(len(first.current_changes), 15)
        second = self.ingest(articles)
        third = self.ingest([{k:v for k,v in reversed(list(a.items()))} for a in articles])
        self.assertNotEqual(first.batch_id, second.batch_id)
        self.assertEqual(second.counts.unchanged, 15)
        self.assertEqual(third.counts.unchanged, 15)
        self.assertEqual(self.count("article_versions"), 15)
        self.assertEqual(self.count("jobs"), 0)

    def test_defaults_and_raw_bytes(self):
        a = deepcopy(self.article)
        a["is_synthetic"] = False; a["content_kind"] = "user_provided"
        first = self.ingest([a])
        del a["is_synthetic"]
        raw = b"\xef\xbb\xbf  " + json.dumps([a], ensure_ascii=False, indent=4).encode() + b"\n"
        second = self.store.import_batch(raw, source_name="incoming.json")
        self.assertEqual(second.counts.unchanged, 1)
        self.assertEqual(self.store.get_batch_bytes(second.batch_id), raw)
        self.assertEqual(second.source_name, "incoming.json")
        self.assertEqual(first.items[0].source_id, source_id_for(a["article_id"], 1))

    def test_text_and_metadata_revisions_preserve_history(self):
        first = self.source()
        original = self.store.get_article(first)
        self.article["text"] += " Исправление."
        second = self.ingest()
        self.assertEqual(second.counts.updated, 1)
        self.assertEqual(second.current_changes[0].previous_source_id, first)
        self.article["published_at"] = "2026-11-03T10:00:00+03:00"
        third = self.ingest()
        self.assertEqual(third.counts.updated, 1)
        self.assertEqual(self.store.get_article(first), original)
        versions = self.store.list_versions(self.article["article_id"])
        self.assertEqual([v.record.article_version for v in versions], [1,2,3])
        self.assertEqual([v.is_current for v in versions], [False,False,True])

    def test_url_title_and_provenance_are_versioned(self):
        self.source()
        for field, value in [("url","https://example.org/news/1"),("title","New title"),("content_kind","user_provided")]:
            self.article[field] = value
            self.assertEqual(self.ingest().counts.updated,1)
        self.assertEqual(len(self.store.list_versions(self.article["article_id"])),4)

    def test_historical_replay_and_explicit_restore(self):
        old = deepcopy(self.article)
        v1 = self.source()
        self.article["text"] += " Version two."
        v2 = self.ingest().items[0].source_id
        replay = self.ingest([old])
        self.assertEqual(replay.counts.historical,1)
        self.assertEqual(replay.items[0].source_id,v1)
        self.assertEqual(replay.current_changes,[])
        self.assertEqual(self.store.get_current_article(old["article_id"]).source_id,v2)
        restore = self.store.restore_version(old["article_id"],1,"Author confirmed the original text")
        self.assertEqual(restore.record.article_version,3)
        self.assertEqual(restore.record.article.model_dump(),old)
        self.assertEqual(restore.previous_source_id,v2)
        self.assertEqual(restore.restored_from_source_id,v1)
        self.assertEqual(self.ingest([old]).counts.unchanged,1)
        self.assertEqual(self.store.list_versions(old["article_id"])[-1].restoration_reason,restore.reason)
        self.assert_error("already_current",self.store.restore_version,old["article_id"],3,"Again")

    def test_unseen_older_publication_is_new_received_revision(self):
        self.source()
        self.article["published_at"] = "2020-01-01T00:00:00+00:00"
        receipt = self.ingest()
        self.assertEqual(receipt.counts.updated,1)
        self.assertEqual(self.store.get_current_article(self.article["article_id"]).article_version,2)

    def test_partial_and_rejected_inputs(self):
        bad = deepcopy(self.article); bad["article_id"]="bad"; bad["text"]=3
        receipt = self.ingest([self.article,bad,None])
        self.assertEqual(receipt.import_status,"partial")
        self.assertEqual(receipt.counts.invalid,2)
        self.assertEqual(receipt.items[1].issues[0].code,"invalid_type_or_value")
        self.assertEqual(receipt.items[1].issues[0].path,"/items/1/text")
        for raw in [b"{",b"{}",b"\xff",b'[NaN]',b'[{"a":1,"a":2}]']:
            rejected=self.store.import_batch(raw)
            self.assertEqual(rejected.import_status,"rejected")
            self.assertTrue(rejected.issues)
            self.assertEqual(self.store.get_batch_bytes(rejected.batch_id),raw)
        all_bad=self.ingest([None])
        self.assertEqual(all_bad.import_status,"rejected")
        self.assertEqual(self.store.import_batch(b"[]").import_status,"completed")

    def test_bad_unicode_or_overflow_in_one_row_does_not_block_other_rows(self):
        bad=deepcopy(self.article); bad["article_id"]="bad"; bad["text"]="\ud800"
        bad_id=deepcopy(self.article); bad_id["article_id"]="\ud800"
        receipt=self.store.import_batch(json.dumps([self.article,bad,bad_id],ensure_ascii=True).encode())
        self.assertEqual((receipt.counts.new,receipt.counts.invalid),(1,2))
        self.assertIn(receipt.items[1].issues[0].code,("invalid_unicode","invalid_type_or_value"))
        self.assertIsNone(receipt.items[2].article_id)
        raw=json.dumps([dict(self.article,article_id="overflow",text=0),self.article]).replace('"text": 0','"text": 1e999').encode()
        self.assertEqual(self.store.import_batch(raw).import_status,"partial")

    def test_conflicting_ids_and_exact_duplicates(self):
        conflicting=deepcopy(self.article); conflicting["text"] += " Different."
        receipt=self.ingest([self.article,conflicting])
        self.assertEqual(receipt.counts.invalid,2)
        self.assertTrue(all(x.issues[0].code=="ambiguous_article_versions" for x in receipt.items))
        self.assertEqual(self.count("article_versions"),0)
        receipt=self.ingest([self.article,deepcopy(self.article)])
        self.assertEqual((receipt.counts.new,receipt.counts.unchanged),(1,1))
        self.assertEqual(self.count("article_versions"),1)

    def test_interrupted_import_resumes_archived_input(self):
        second=deepcopy(self.article); second["article_id"]="second"
        original=self.store._import_item
        def interrupted(batch,index,*args):
            if index==1: raise KeyboardInterrupt()
            return original(batch,index,*args)
        source_path=Path(self.temp.name)/"input.json"
        source_path.write_text(json.dumps([self.article,second]))
        with patch.object(self.store,"_import_item",side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): self.store.import_batch(source_path)
        receipt=self.store.list_batches(import_status="importing")[0]
        self.assertEqual((receipt.counts.new,receipt.counts.pending),(1,1))
        source_path.write_text("not json anymore")
        reopened=EventStore(self.path,clock=self.clock)
        done=reopened.resume_batch(receipt.batch_id)
        self.assertEqual(done.counts.new,2)
        self.assertEqual(done.counts.pending,0)
        self.assertEqual(self.count("article_versions"),2)
        self.assertEqual(reopened.resume_batch(receipt.batch_id),done)

    def test_version_and_item_are_one_transaction(self):
        original=self.store._create_version
        def interrupted(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError("Crash before receipt")
        with patch.object(self.store,"_create_version",side_effect=interrupted):
            with self.assertRaises(RuntimeError): self.ingest()
        self.assertEqual(self.count("article_versions"),0)
        self.assertEqual(self.count("article_heads"),0)
        self.assertEqual(self.count("batch_items"),0)
        receipt=self.store.list_batches(import_status="importing")[0]
        self.assertEqual(self.store.resume_batch(receipt.batch_id).counts.new,1)

    def test_failed_revision_does_not_revoke_current_attempt(self):
        job=self.running()
        self.article["text"] += " A new version."
        original=self.store._create_version
        def crash(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError("Crash after invalidation but before commit")
        with patch.object(self.store,"_create_version",side_effect=crash):
            with self.assertRaises(RuntimeError): self.ingest()
        self.assertEqual(self.store.get_current_article(self.article["article_id"]).source_id,job.source_id)
        self.assertEqual(self.store.get_job(job.job_id).state,"running")
        self.assertEqual(self.store.complete_attempt(job.active_attempt.token,{}).state,"succeeded")

    def test_historical_records_support_b1_quotes(self):
        first=self.source()
        bundle=deepcopy(next(b for b in self.synthetic["bundles"] if b["event"]["event_id"]=="synthetic:meeting20"))
        bundle["claims"]=bundle["claims"][:1]; bundle["sources"]=bundle["sources"][:1]
        bundle["evidence"]=bundle["evidence"][:1]; bundle["conflicts"]=[]; bundle["relations"]=[]
        bundle["sources"][0]["source_id"]=first; bundle["evidence"][0]["source_id"]=first
        self.article["text"]="Changed text."
        self.ingest()
        validate_grounding(bundle,[self.store.get_article(first)])

    def test_idempotent_enqueue_and_configuration_hash(self):
        source=self.source()
        first=self.store.enqueue(source,self.spec)
        second=self.store.enqueue(source,self.spec.model_dump())
        self.assertEqual(first.job_id,second.job_id)
        other=self.spec.model_copy(deep=True)
        other.configuration["prompt_version"]="2"
        self.assertNotEqual(self.store.enqueue(source,other).job_id,first.job_id)
        self.assertEqual(self.count("jobs"),2)
        reverse={k:v for k,v in reversed(list(self.spec.configuration.items()))}
        self.assertEqual(processing_hash(self.spec),processing_hash(self.spec.model_copy(update={"configuration":reverse})))
        self.assertIsNone(self.store.get_cached_result(source,self.spec))

    def test_processing_spec_rejects_non_json(self):
        source=self.source()
        bad=self.spec.model_copy(update={"configuration":{"temperature":float("nan")}})
        self.assert_error("invalid_json_payload",self.store.enqueue,source,bad)
        bad=self.spec.model_dump(); bad["configuration"]={"bad":object()}
        self.assert_error("invalid_argument",self.store.enqueue,source,bad)

    def test_exact_cache_and_intermediate_artifacts(self):
        job=self.running(); token=job.active_attempt.token
        artifact=self.store.save_artifact(token,"raw_response","not even valid model JSON",schema_version=None)
        self.assertEqual(self.store.get_artifact(artifact.artifact_id),artifact)
        done=self.store.complete_attempt(token,{"claims":[]},schema_version="draft:1")
        self.assertEqual(done.state,"succeeded")
        cached=self.store.get_cached_result(job.source_id,self.spec)
        self.assertEqual(cached.payload,{"claims":[]})
        self.assertEqual(done.result_artifact_id,cached.artifact_id)
        self.assertEqual(len(self.store.list_artifacts(job.active_attempt.attempt_id)),2)
        self.assertEqual(self.store.enqueue(job.source_id,self.spec).job_id,job.job_id)
        other=self.spec.model_copy(update={"processor_version":"2"})
        self.assertIsNone(self.store.get_cached_result(job.source_id,other))
        self.assert_error("stale_attempt",self.store.complete_attempt,token,{"late":True})

    def test_failure_and_explicit_retry(self):
        job=self.running(); token=job.active_attempt.token
        self.store.save_artifact(token,"raw_response","bad output")
        failed=self.store.fail_attempt(token,[Issue(code="invalid_model_output",path="/",message="Bad output")])
        self.assertEqual(failed.state,"failed")
        self.assertIsNone(self.store.get_cached_result(job.source_id,self.spec))
        self.assertIsNone(self.store.claim_next(self.spec,"worker"))
        self.assertEqual(self.store.retry_job(job.job_id).state,"pending")
        retry=self.store.claim_next(self.spec,"worker")
        self.assertEqual(retry.active_attempt.attempt_number,2)
        self.assertNotEqual(retry.active_attempt.token,token)
        self.assert_error("stale_attempt",self.store.complete_attempt,token,{"late":True})
        self.assertEqual(self.store.list_attempts(job.job_id)[0].issues[0].code,"invalid_model_output")

    def test_defer_without_attempt_and_active_defer(self):
        source=self.source(); job=self.store.enqueue(source,self.spec)
        self.assertEqual(self.store.defer_job(job.job_id,"Budget not available").state,"deferred")
        self.assertEqual(self.store.list_attempts(job.job_id),[])
        self.assertIsNone(self.store.claim_next(self.spec,"worker"))
        self.store.retry_job(job.job_id)
        active=self.store.claim_next(self.spec,"worker")
        self.assert_error("attempt_not_found",self.store.defer_job,job.job_id,"Stop")
        self.assertEqual(self.store.defer_job(job.job_id,"Stop",token=active.active_attempt.token).state,"deferred")
        self.assertEqual(self.store.list_attempts(job.job_id)[0].status,"deferred")

    def test_claim_is_atomic_between_connections(self):
        source=self.source(); self.store.enqueue(source,self.spec)
        other=EventStore(self.path,clock=self.clock)
        barrier=Barrier(2)
        def claim(store):
            barrier.wait()
            return store.claim_next(self.spec,"worker")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(claim,[self.store,other]))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.count("attempts"),1)

    def test_claim_requires_exact_spec(self):
        self.store.enqueue(self.source(),self.spec)
        different=self.spec.model_copy(update={"processor_version":"99"})
        self.assertIsNone(self.store.claim_next(different,"worker"))

    def test_expiration_renewal_and_stale_token(self):
        job=self.running(); token=job.active_attempt.token
        self.clock.advance(800)
        renewed=self.store.renew_lease(token)
        self.clock.advance(101)
        self.assertEqual(self.store.requeue_expired(),[])
        self.clock.advance(800)
        self.assert_error("stale_attempt",self.store.complete_attempt,token,{})
        self.assert_error("stale_attempt",self.store.renew_lease,token)
        self.assertEqual(self.store.requeue_expired()[0].state,"pending")
        self.assertEqual(self.store.list_attempts(job.job_id)[0].status,"interrupted")
        active=self.store.claim_next(self.spec,"restarted-worker")
        self.assertEqual(active.active_attempt.attempt_number,2)
        self.assertEqual(self.store.requeue_expired(),[])
        self.assert_error("stale_attempt",self.store.save_artifact,token,"late","response")

    def test_revision_supersedes_pending_running_and_deferred(self):
        source=self.source()
        pending=self.store.enqueue(source,self.spec)
        running_spec=self.spec.model_copy(update={"processor_version":"2"})
        self.store.enqueue(source,running_spec)
        active=self.store.claim_next(running_spec,"worker")
        deferred=self.store.enqueue(source,self.spec.model_copy(update={"processor_version":"3"}))
        self.store.defer_job(deferred.job_id,"Later")
        self.article["text"] += " Corrected."
        new_source=self.ingest().items[0].source_id
        for j in [pending,active,deferred]:
            self.assertEqual(self.store.get_job(j.job_id).state,"superseded")
        self.assertEqual(self.store.list_attempts(active.job_id)[0].status,"superseded")
        self.assert_error("stale_attempt",self.store.complete_attempt,active.active_attempt.token,{"obsolete":True})
        self.assertIsNone(self.store.claim_next(self.spec,"worker"))
        self.assert_error("article_not_current",self.store.enqueue,source,self.spec)
        self.assertIsNone(self.store.get_cached_result(new_source,self.spec))
        self.assertEqual(self.store.enqueue(new_source,self.spec).state,"pending")

    def test_historical_success_is_retained_for_audit(self):
        job=self.running(); done=self.store.complete_attempt(job.active_attempt.token,{"old":True})
        self.article["text"] += " Corrected."
        self.ingest()
        self.assertFalse(self.store.get_job(job.job_id).is_current)
        self.assertEqual(self.store.get_job(job.job_id).state,"succeeded")
        self.assertEqual(self.store.get_artifact(done.result_artifact_id).payload,{"old":True})
        self.assertEqual(self.store.get_cached_result(job.source_id,self.spec).payload,{"old":True})

    def test_restore_invalidates_current_attempt(self):
        self.source(); self.article["text"] += " Second."
        current=self.ingest().items[0].source_id
        job=self.store.enqueue(current,self.spec)
        active=self.store.claim_next(self.spec,"worker")
        self.store.restore_version(self.article["article_id"],1,"Confirmed")
        self.assertEqual(self.store.get_job(job.job_id).state,"superseded")
        self.assert_error("stale_attempt",self.store.complete_attempt,active.active_attempt.token,[])

    def test_success_artifact_and_status_roll_back_together(self):
        job=self.running(); token=job.active_attempt.token
        original=self.store._insert_artifact
        def crash(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError("Crash after result insertion")
        with patch.object(self.store,"_insert_artifact",side_effect=crash):
            with self.assertRaises(RuntimeError): self.store.complete_attempt(token,{"result":1})
        self.assertEqual(self.count("artifacts"),0)
        self.assertEqual(self.store.get_job(job.job_id).state,"running")
        self.assertIsNone(self.store.get_cached_result(job.source_id,self.spec))
        self.assertEqual(self.store.complete_attempt(token,{"result":1}).state,"succeeded")

    def test_missing_result_and_invalid_artifact_do_not_complete_job(self):
        job=self.running(); token=job.active_attempt.token
        self.assert_error("missing_result",self.store.complete_attempt,token,None)
        self.assert_error("invalid_json_payload",self.store.complete_attempt,token,{"number":float("inf")})
        self.assert_error("invalid_json_payload",self.store.save_artifact,token,"raw",{1:"bad key"})
        self.assertEqual(self.store.get_job(job.job_id).state,"running")
        self.assertEqual(self.count("artifacts"),0)
        self.store.complete_attempt(token,[])

    def test_immutable_archive_and_artifact(self):
        job=self.running(); artifact=self.store.save_artifact(job.active_attempt.token,"raw","original")
        with sqlite3.connect(self.path) as conn:
            for sql,params in [("UPDATE article_versions SET payload_json='{}'",()),
                               ("DELETE FROM article_versions",()),
                               ("UPDATE artifacts SET payload_json='null' WHERE artifact_id=?",(artifact.artifact_id,)),
                               ("UPDATE batches SET raw_bytes=?",(b'[]',)),
                               ("DELETE FROM batch_items",())]:
                with self.assertRaises(sqlite3.IntegrityError): conn.execute(sql,params)
        self.assertEqual(self.store.get_artifact(artifact.artifact_id).payload,"original")

    def test_reopen_and_database_pragmas(self):
        source=self.source(); reopened=EventStore(self.path,clock=self.clock)
        self.assertEqual(reopened.get_article(source),self.store.get_article(source))
        with reopened._transaction() as conn:
            self.assertEqual(conn.execute("PRAGMA application_id").fetchone()[0],APPLICATION_ID)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0],"wal")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0],2)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0],1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0],5000)

    def test_foreign_database_and_newer_schema_are_not_recreated(self):
        foreign=Path(self.temp.name)/"foreign.sqlite3"
        with sqlite3.connect(foreign) as conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
            conn.execute("INSERT INTO unrelated VALUES ('keep')")
        self.assert_error("unsupported_storage_schema",EventStore,foreign)
        with sqlite3.connect(foreign) as conn:
            self.assertEqual(conn.execute("SELECT value FROM unrelated").fetchone()[0],"keep")
            self.assertEqual(conn.execute("PRAGMA application_id").fetchone()[0],0)
        with sqlite3.connect(self.path) as conn: conn.execute("PRAGMA user_version=2")
        self.assert_error("unsupported_storage_schema",EventStore,self.path)

    def test_storage_busy_without_sleep(self):
        impatient=EventStore(self.path,clock=self.clock,busy_timeout_ms=0)
        conn=sqlite3.connect(self.path,isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("BEGIN IMMEDIATE")
        self.assert_error("storage_busy",impatient.import_batch,b"[]")
        conn.rollback()
        self.assertEqual(impatient.import_batch(b"[]").import_status,"completed")

    def test_job_state_guards_and_wrong_token(self):
        job=self.running()
        self.assert_error("invalid_job_state",self.store.retry_job,job.job_id)
        self.assert_error("invalid_argument",self.store.renew_lease,job.active_attempt.token,lease_seconds=0)
        self.assert_error("attempt_not_found",self.store.complete_attempt,"unknown",{})
        self.assert_error("job_not_found",self.store.get_job,"unknown")
        self.assert_error("article_not_found",self.store.get_article,"unknown")
        self.assert_error("batch_not_found",self.store.get_batch,"unknown")

    def test_cli_import_show_enqueue_and_errors(self):
        raw=Path(self.temp.name)/"input.json"; raw.write_text(json.dumps([self.article]))
        spec=Path(self.temp.name)/"spec.json"; spec.write_text(self.spec.model_dump_json())
        def cli(*args):
            result=subprocess.run([sys.executable,"-m","llm.events.storage_cli","--db",str(self.path),*map(str,args)],cwd=ROOT,capture_output=True,text=True)
            return result,json.loads(result.stdout)
        result,receipt=cli("import-batch",raw)
        self.assertEqual(result.returncode,0,result.stderr)
        result,shown=cli("show-batch",receipt["batch_id"])
        self.assertEqual(receipt,shown)
        result,queued=cli("enqueue","--batch",receipt["batch_id"],"--spec",spec)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(queued["jobs"][0]["state"],"pending")
        raw.write_text("invalid")
        result,rejected=cli("import-batch",raw)
        self.assertEqual(result.returncode,1)
        self.assertEqual(rejected["import_status"],"rejected")
        result,error=cli("show-job","missing")
        self.assertEqual(result.returncode,1)
        self.assertEqual(error["issues"][0]["code"],"job_not_found")


if __name__ == "__main__":
    unittest.main()
