"""Immutable snapshot runs and transactional sequential cluster checkpoints."""

import hashlib
import json
import sqlite3
import tempfile
import shutil
import os
import fcntl
from contextlib import contextmanager, closing
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL
from .runtime import EventStore, ProviderProfile, canonical_json, execute, request
from .blocks import source_blocks, fit_packets
from .extraction import parse_mentions, PROMPT as EXTRACT_PROMPT
from .clustering import parse_decisions, attachment, PROMPT as CLUSTER_PROMPT
from .models import EventMention, EventCluster
from .retrieval import BM25Retriever


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def read(path):
    value = json.loads(Path(path).read_text())
    if Path(path).name == "plan.json" and value.get("run_id") != digest(
        {k: v for k, v in value.items() if k != "run_id"}
    ):
        raise ValueError("plan_hash_mismatch")
    return value


def plan_run(corpus, output, document_ids=None, configuration=None):
    corpus, root = Path(corpus), Path(output)
    manifest = read(corpus / "manifest.json")
    if manifest["schema_version"] != "prepared-news.v1":
        raise ValueError("unsupported_corpus")
    raw = (corpus / "documents.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["outputs"]["documents.jsonl"]:
        raise ValueError("corpus_hash_mismatch")
    docs = [json.loads(line) for line in raw.splitlines() if line.strip()]
    from llm.events.contracts.models import ArticleInput
    from llm.events.contracts.validation import article_sha256

    for d in docs:
        article = ArticleInput.model_validate(d["article"])
        if article_sha256(article) != d["article_sha256"]:
            raise ValueError("article_hash_mismatch")
    if document_ids is not None:
        wanted = set(document_ids)
        if wanted - {d["document_id"] for d in docs}:
            raise ValueError("unknown_document")
        docs = [d for d in docs if d["document_id"] in wanted]
    if len({d["document_id"] for d in docs}) != len(docs):
        raise ValueError("duplicate_document_id")
    if len({d["article"]["article_id"] for d in docs}) != len(docs):
        raise ValueError("select_one_revision")
    config = {"block_limit": 2000, "packet_limit": 8000, **(configuration or {})}
    profile = ProviderProfile(
        execution_mode="offline",
        limits_confirmed=True,
        max_input_tokens=30000,
        max_output_tokens=4096,
        context_window_tokens=34096,
    ).model_dump(mode="json")
    config.setdefault("extraction_profile", profile)
    config.setdefault("verification_profile", profile)

    def order(d):
        date = d["article"].get("published_at")
        if date:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            local = datetime.fromisoformat(date).astimezone(ZoneInfo("Europe/Moscow"))
            return (0, local.date().isoformat(), 1, local.timestamp(), d["document_id"])
        dates = {p.get("publication", {}).get("date") for p in d["provenance"]} - {None}
        return (
            (0, next(iter(dates)), 0, 0, d["document_id"])
            if len(dates) == 1
            else (1, "", 0, 0, d["document_id"])
        )

    docs.sort(key=order)
    implementation = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).parent.glob("*.py")
    }
    plan = {
        "schema_version": "event-pipeline.v1",
        "corpus_id": manifest["corpus_id"],
        "documents": docs,
        "configuration": config,
        "implementation": implementation,
    }
    extraction_profile = ProviderProfile.model_validate(config["extraction_profile"])
    plan["extraction_packets"] = {}
    for d in docs:
        bs = source_blocks(d, config["block_limit"])
        metadata = {k: v for k, v in d["article"].items() if k != "text"}
        plan["extraction_packets"][d["document_id"]] = list(
            fit_packets(
                bs,
                config["packet_limit"],
                extraction_profile.max_request_bytes,
                lambda packet: request(
                    extraction_profile, EXTRACT_PROMPT, {"article": metadata, **packet}
                ),
            )
        )
    plan["run_id"] = digest(plan)
    if root.exists():
        if read(root / "plan.json") != plan:
            raise ValueError("run_exists_with_different_inputs")
        return plan
    destination = root
    destination.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=".plan-", dir=destination.parent))
    try:
        (root / "plan.json").write_text(canonical_json(plan))
        store = EventStore(root / "jobs.sqlite3")
        receipt = store.import_batch(
            canonical_json([d["article"] for d in docs]).encode()
        )
        if receipt.counts.invalid:
            raise ValueError("snapshot_import_invalid")
        with closing(sqlite3.connect(root / "catalog.sqlite3")) as db, db:
            db.executescript(
                "PRAGMA application_id=1162889552; PRAGMA user_version=1; CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE extraction(document_id TEXT PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE steps(position INTEGER PRIMARY KEY,payload TEXT NOT NULL); CREATE TRIGGER step_immutable BEFORE UPDATE ON steps BEGIN SELECT RAISE(ABORT,'immutable step'); END; CREATE TRIGGER step_no_delete BEFORE DELETE ON steps BEGIN SELECT RAISE(ABORT,'immutable step'); END;"
            )
            db.execute("INSERT INTO state VALUES (?,?)", ("phase", "extracting"))
        os.rename(root, destination)
    except BaseException:
        shutil.rmtree(root)
        raise
    return plan


@contextmanager
def locked(root):
    root = Path(root)
    with (root / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("run_busy") from None
        try:
            yield root
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def connect(root):
    db = sqlite3.connect(Path(root) / "catalog.sqlite3", timeout=5)
    if (
        db.execute("PRAGMA application_id").fetchone()[0],
        db.execute("PRAGMA user_version").fetchone()[0],
    ) != (1162889552, 1):
        db.close()
        raise ValueError("unsupported_catalog_schema")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        with db:
            yield db
    finally:
        db.close()


def snapshot_article(store, document):
    from llm.events.contracts.validation import article_sha256

    record = store.get_current_article(document["article"]["article_id"])
    if article_sha256(record.article) != document["article_sha256"]:
        raise ValueError("snapshot_article_changed")
    return record


def assert_implementation(plan):
    current = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path(__file__).parent.glob("*.py")
    }
    if current != plan["implementation"]:
        raise ValueError("implementation_changed_create_new_run")


def extract_mentions(root, provider, max_calls, limits=None, journal=None):
    if type(max_calls) is not int or max_calls < 1:
        raise ValueError("invalid_max_calls")
    with locked(root) as root:
        plan = read(root / "plan.json")
        assert_implementation(plan)
        cfg = plan["configuration"]
        profile = ProviderProfile.model_validate(cfg["extraction_profile"])
        store = EventStore(root / "jobs.sqlite3")
        used = 0
        with connect(root) as db:
            if (
                db.execute("SELECT value FROM state WHERE key='phase'").fetchone()[0]
                != "extracting"
            ):
                raise ValueError("extraction_frozen")
            saved = {
                did: json.loads(payload)
                for did, payload in db.execute("SELECT * FROM extraction")
            }
            done = {did for did, payload in saved.items() if payload.get("complete")}
        for doc in plan["documents"]:
            did = doc["document_id"]
            if did in done:
                continue
            previous = saved.get(did, {})
            results = list(previous.get("packets", []))
            mentions = {m["mention_id"]: m for m in previous.get("mentions", [])}
            complete = True
            for packet in plan["extraction_packets"][did][len(results) :]:
                if packet.get("deferred_reason"):
                    results.append(
                        {
                            "status": "deferred",
                            "reason": packet["deferred_reason"],
                            "coverage": dict.fromkeys(packet["primary"], "deferred"),
                        }
                    )
                    continue
                if used >= max_calls:
                    complete = False
                    break
                req = request(
                    profile,
                    EXTRACT_PROMPT,
                    {
                        "article": {
                            k: v for k, v in doc["article"].items() if k != "text"
                        },
                        **packet,
                    },
                )
                record = snapshot_article(store, doc)
                result = execute(
                    store,
                    record.source_id,
                    "extract",
                    req,
                    profile,
                    provider,
                    limits,
                    journal,
                )
                used += result["calls"]
                if result["status"] in ("succeeded", "cached"):
                    content = store.get_artifact(result["artifact_id"]).payload[
                        "content"
                    ]
                    try:
                        audit = parse_mentions(content, did, packet)
                        for m in audit.mentions:
                            mentions[m.mention_id] = m.model_dump()
                        result["audit"] = audit.model_dump()
                    except ValueError as exc:
                        result["parse_error"] = str(exc)
                result["primary"] = packet["primary"]
                if "audit" not in result:
                    result["coverage"] = dict.fromkeys(
                        packet["primary"],
                        "deferred" if result["status"] == "deferred" else "missed",
                    )
                results.append(result)
                with connect(root) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO extraction VALUES (?,?)",
                        (
                            did,
                            canonical_json(
                                {
                                    "mentions": list(mentions.values()),
                                    "packets": results,
                                    "complete": False,
                                }
                            ),
                        ),
                    )
                if result.get("stop"):
                    complete = False
                    break
            if complete:
                with connect(root) as db:
                    db.execute(
                        "INSERT OR REPLACE INTO extraction VALUES (?,?)",
                        (
                            did,
                            canonical_json(
                                {
                                    "mentions": list(mentions.values()),
                                    "packets": results,
                                    "complete": True,
                                }
                            ),
                        ),
                    )
            else:
                break
        return status(root)


def load_mentions(root):
    plan = read(Path(root) / "plan.json")
    with connect(root) as db:
        data = {
            did: json.loads(payload)
            for did, payload in db.execute("SELECT * FROM extraction")
        }
    result = []
    for d in plan["documents"]:
        positions = {
            b.block_id: b.start
            for b in source_blocks(d, plan["configuration"]["block_limit"])
        }
        local = [
            EventMention.model_validate(m)
            for m in data.get(d["document_id"], {}).get("mentions", [])
        ]
        result.extend(
            sorted(
                local,
                key=lambda m: (min(positions[b] for b in m.block_ids), m.mention_id),
            )
        )
    return result


def cluster_mentions(
    root, provider, max_calls, limits=None, allow_partial=False, journal=None
):
    if type(max_calls) is not int or max_calls < 1:
        raise ValueError("invalid_max_calls")
    with locked(root) as root:
        plan = read(root / "plan.json")
        assert_implementation(plan)
        docs = {d["document_id"]: d for d in plan["documents"]}
        cfg = plan["configuration"]
        profile = ProviderProfile.model_validate(cfg["verification_profile"])
        mentions = load_mentions(root)
        by_id = {m.mention_id: m for m in mentions}
        blocks = {
            b.block_id: b
            for d in docs.values()
            for b in source_blocks(d, cfg["block_limit"])
        }
        with connect(root) as db:
            count = sum(
                json.loads(p).get("complete", False)
                for (p,) in db.execute("SELECT payload FROM extraction")
            )
            if count != len(docs) and not allow_partial:
                raise ValueError("extraction_incomplete")
            frozen = digest([m.model_dump() for m in mentions])
            old = db.execute(
                "SELECT value FROM state WHERE key='mentions_hash'"
            ).fetchone()
            if old and old[0] != frozen:
                raise ValueError("frozen_mentions_changed")
            db.execute(
                "INSERT OR IGNORE INTO state VALUES ('mentions_hash',?)", (frozen,)
            )
            db.execute("UPDATE state SET value='clustering' WHERE key='phase'")
            steps = [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM steps ORDER BY position")
            ]
        clusters = {}
        memberships = {}
        for s in steps:
            clusters[s["cluster"]["cluster_id"]] = s["cluster"]
            memberships[s["mention_id"]] = s["cluster"]["cluster_id"]
        store = EventStore(root / "jobs.sqlite3")
        used = 0
        for position, m in enumerate(mentions[len(steps) :], len(steps)):
            if used >= max_calls:
                break
            retrieval = BM25Retriever().search(
                m, mentions[:position], blocks, memberships
            )
            candidates = retrieval["cluster_ids"]
            decisions = []
            reason = "no_candidates"
            call = None
            if candidates:
                ids = {m.mention_id} | {
                    mid for cid in candidates for mid in clusters[cid]["mention_ids"]
                }
                bids = {
                    bid
                    for mid in ids
                    for bid in by_id[mid].block_ids + by_id[mid].context_block_ids
                }
                data = {
                    "new_mention": m.model_dump(),
                    "clusters": [clusters[c] for c in candidates],
                    "mentions": [by_id[mid].model_dump() for mid in sorted(ids)],
                    "blocks": [blocks[b].model_dump() for b in sorted(bids)],
                    "metadata": {
                        by_id[mid].document_id: {
                            k: v
                            for k, v in docs[by_id[mid].document_id]["article"].items()
                            if k != "text"
                        }
                        for mid in ids
                    },
                }
                req = request(profile, CLUSTER_PROMPT, data)
                if len(canonical_json(req).encode()) > profile.max_request_bytes:
                    reason = "context_too_large"
                else:
                    rec = snapshot_article(store, docs[m.document_id])
                    call = execute(
                        store,
                        rec.source_id,
                        "cluster",
                        req,
                        profile,
                        provider,
                        limits,
                        journal,
                    )
                    used += call["calls"]
                    if call["status"] in ("cached", "succeeded"):
                        try:
                            decisions = parse_decisions(
                                store.get_artifact(call["artifact_id"]).payload[
                                    "content"
                                ],
                                candidates,
                                ids,
                                bids,
                            )
                            for decision in decisions:
                                if m.mention_id not in decision.mention_ids or not set(
                                    decision.mention_ids
                                ).intersection(
                                    clusters[decision.cluster_id]["mention_ids"]
                                ):
                                    raise ValueError("missing_pair_evidence")
                                allowed = {
                                    m.mention_id,
                                    *clusters[decision.cluster_id]["mention_ids"],
                                }
                                if not set(decision.mention_ids) <= allowed:
                                    raise ValueError("unrelated_pair_evidence")
                                old_blocks = {
                                    b
                                    for mid in clusters[decision.cluster_id][
                                        "mention_ids"
                                    ]
                                    for b in by_id[mid].block_ids
                                    + by_id[mid].context_block_ids
                                }
                                if not set(decision.block_ids) <= old_blocks | set(
                                    m.block_ids + m.context_block_ids
                                ):
                                    raise ValueError("unrelated_pair_blocks")
                                if not set(decision.block_ids).intersection(
                                    m.block_ids + m.context_block_ids
                                ) or not set(decision.block_ids).intersection(
                                    old_blocks
                                ):
                                    raise ValueError("missing_pair_blocks")
                            reason = "assessed"
                        except ValueError:
                            decisions = []
                            reason = "invalid_verification"
                    else:
                        reason = call.get("reason") or call["status"]
            target = attachment(decisions)
            cid = target or str(
                uuid5(NAMESPACE_URL, plan["run_id"] + ":" + m.mention_id)
            )
            cluster = (
                EventCluster.model_validate(clusters[cid])
                if target
                else EventCluster(cluster_id=cid, mention_ids=[], label=m.gloss or cid)
            )
            cluster.mention_ids.append(m.mention_id)
            cluster.decisions.extend(decisions)
            cluster.verification_status = (
                "assessed"
                if target
                or decisions
                and all(d.identity == "different" for d in decisions)
                else "not_required"
                if not candidates
                else "uncertain"
                if decisions
                else "unassessed"
            )
            if target:
                cluster.version += 1
            step = {
                "mention_id": m.mention_id,
                "cluster": cluster.model_dump(),
                "reason": reason,
                "retrieval": retrieval,
                "call": call,
            }
            with connect(root) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO steps VALUES (?,?)", (position, canonical_json(step))
                )
            clusters[cid] = cluster.model_dump()
            memberships[m.mention_id] = cid
            if call and call.get("stop"):
                break
        with connect(root) as db:
            if db.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == len(mentions):
                db.execute("UPDATE state SET value='complete' WHERE key='phase'")
        return status(root)


def status(root):
    plan = read(Path(root) / "plan.json")
    with connect(root) as db:
        return {
            "run_id": plan["run_id"],
            "documents": len(plan["documents"]),
            "extracted_documents": sum(
                json.loads(p).get("complete", False)
                for (p,) in db.execute("SELECT payload FROM extraction")
            ),
            "clustered_mentions": db.execute("SELECT COUNT(*) FROM steps").fetchone()[
                0
            ],
            "mentions": len(load_mentions(root)),
            "phase": db.execute("SELECT value FROM state WHERE key='phase'").fetchone()[
                0
            ],
        }


def resume_run(root, provider, max_calls, **kwargs):
    return (
        extract_mentions if status(root)["phase"] == "extracting" else cluster_mentions
    )(root, provider, max_calls, **kwargs)


def retrieve_candidates(root, position):
    mentions = load_mentions(root)
    plan = read(Path(root) / "plan.json")
    blocks = {
        b.block_id: b
        for d in plan["documents"]
        for b in source_blocks(d, plan["configuration"]["block_limit"])
    }
    with connect(root) as db:
        steps = [
            json.loads(r[0])
            for r in db.execute("SELECT payload FROM steps ORDER BY position")
        ]
    memberships = {s["mention_id"]: s["cluster"]["cluster_id"] for s in steps}
    return BM25Retriever().search(
        mentions[position], mentions[:position], blocks, memberships
    )
