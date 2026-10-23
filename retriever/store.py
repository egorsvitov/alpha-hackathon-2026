"""Transactional search representations over an immutable source archive."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading

from .models import (ApplyResult, ArticleRevision, ClusterSnapshot, EncodedItem,
                     IndexSnapshot, SearchItem, VectorRecord, canonical, digest, text_hash)
from llm.cluster_merge import core
from llm.cluster_merge.models import Article, MergeRequest, MergeResult


def _validate(cls, value):
    # Pydantic normally trusts existing instances, whose nested lists can mutate.
    return cls.model_validate(value.model_dump() if hasattr(value, "model_dump") else value)


class SQLiteSearchStore:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=30)
        self._db.execute('PRAGMA busy_timeout=30000')
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, payload TEXT NOT NULL, active INTEGER NOT NULL, original INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY, item_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS clusters(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cluster_members(revision_id TEXT NOT NULL, cluster_id TEXT NOT NULL, PRIMARY KEY(revision_id,cluster_id));
        CREATE TABLE IF NOT EXISTS cluster_history(id TEXT NOT NULL, revision TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY(id,revision));
        CREATE TABLE IF NOT EXISTS applications(id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL, revision TEXT NOT NULL, payload TEXT NOT NULL, item_ids TEXT NOT NULL);
        ''')
        # Backfill older stores and serialize migration with membership updates.
        with self._transaction(True) as db:
            clusters = [ClusterSnapshot.model_validate_json(r[0]) for r in db.execute('SELECT payload FROM clusters')]
            self._write_cluster_members(db, clusters)

    @staticmethod
    def _write_cluster_members(db, clusters):
        db.execute('DELETE FROM cluster_members')
        db.executemany('INSERT INTO cluster_members VALUES (?,?)',
                       [(member, c.cluster_id) for c in clusters for member in c.member_revision_ids])

    def close(self):
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _transaction(self, write=False):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    @staticmethod
    def _config(db):
        values = dict(db.execute('SELECT key,value FROM metadata'))
        if not values:
            raise ValueError('store_not_initialized')
        return json.loads(values['config']), int(values['generation'])

    @staticmethod
    def _bump(db, generation):
        db.execute("UPDATE metadata SET value=? WHERE key='generation'", (str(generation + 1),))

    @staticmethod
    def _insert_immutable(db, table, id_, payload, extra=()):
        old = db.execute(f'SELECT payload FROM {table} WHERE id=?', (id_,)).fetchone()
        if old:
            if old[0] != payload:
                raise ValueError(f'immutable_{table}_conflict:{id_}')
            return False
        placeholders = ','.join('?' for _ in (id_, payload, *extra))
        db.execute(f'INSERT INTO {table} VALUES ({placeholders})', (id_, payload, *extra))
        return True

    @staticmethod
    def _validate_items(items, vectors, sources, dimension, source_by_id=None):
        if len({i.item_id for i in items}) != len(items):
            raise ValueError('duplicate_item_id')
        if len({v.vector_id for v in vectors}) != len(vectors):
            raise ValueError('duplicate_vector_id')
        byid = {i.item_id: i for i in items}
        counts = Counter()
        for item in items:
            if not set(item.source_revision_ids) <= sources:
                raise ValueError('unknown_source_revision')
            if item.kind == 'article':
                if len(item.source_revision_ids) != 1:
                    raise ValueError('article_requires_single_source')
                if item.was_merged or item.publication_count != 1 or item.source_count > 1:
                    raise ValueError('invalid_original_article_counts')
                source = source_by_id[item.source_revision_ids[0]] if source_by_id is not None else None
                if source is not None:
                    embeddings = {source.text}
                    if source.title_origin == 'source' and source.title:
                        embeddings.add(source.title + '\n\n' + source.text)
                    if item.text != source.text or item.title != source.title or item.embedding_text not in embeddings:
                        raise ValueError('article_source_content_mismatch')
                proofs = item.derived_from.get('fragment_texts', {})
                hashes = item.derived_from.get('fragment_text_hashes', [])
                if (not isinstance(proofs, dict) or not isinstance(hashes, list)
                    or any(not isinstance(h, str) for h in hashes)
                    or set(proofs) != set(hashes) or len(set(hashes)) != len(hashes)):
                    raise ValueError('fragment_proof_membership_mismatch')
                for hash_, fragment in proofs.items():
                    if (not isinstance(fragment, str) or not fragment.strip() or text_hash(fragment) != hash_
                        or fragment not in (source.text if source is not None else item.text)):
                        raise ValueError('invalid_fragment_source_proof')
        for v in vectors:
            item = byid.get(v.item_id)
            if item is None or v.item_version != item.item_version:
                raise ValueError('vector_item_mismatch')
            if len(v.values) != dimension:
                raise ValueError('vector_dimension_mismatch')
            allowed = {text_hash(item.embedding_text)}
            if item.kind == 'article':
                allowed.update(item.derived_from.get('fragment_text_hashes', []))
            if v.embedding_text_sha256 not in allowed:
                raise ValueError('vector_text_hash_mismatch')
            counts[v.item_id] += 1
        if set(counts) != set(byid):
            raise ValueError('missing_item_vectors')

    def import_bundle(self, *, articles, items, vectors, clusters, encoder_fingerprint, dimension, corpus_id):
        articles = [_validate(ArticleRevision, a) for a in articles]
        items = [_validate(SearchItem, i) for i in items]
        vectors = [_validate(VectorRecord, v) for v in vectors]
        clusters = [_validate(ClusterSnapshot, c) for c in clusters]
        if not encoder_fingerprint or not corpus_id or isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 1:
            raise ValueError('invalid_index_configuration')
        if len({a.revision_id for a in articles}) != len(articles):
            raise ValueError('duplicate_revision_id')
        sourceids = {a.revision_id for a in articles}
        self._validate_items(items, vectors, sourceids, dimension, {a.revision_id: a for a in articles})
        if any(i.kind != 'article' for i in items):
            raise ValueError('import_requires_original_articles')
        config = dict(corpus_id=corpus_id, encoder_fingerprint=encoder_fingerprint, dimension=dimension)
        with self._transaction(True) as db:
            initialized = db.execute('SELECT 1 FROM metadata').fetchone()
            if initialized:
                oldconfig, generation = self._config(db)
                if config != oldconfig:
                    raise ValueError('index_configuration_mismatch')
            else:
                generation = 0
                db.executemany('INSERT INTO metadata VALUES (?,?)', [('config', canonical(config)), ('generation', '0')])
            changed = False
            for a in articles:
                changed |= self._insert_immutable(db, 'sources', a.revision_id, canonical(a))
            for i in items:
                changed |= self._insert_immutable(db, 'items', i.item_id, canonical(i), (1, 1))
            for v in vectors:
                old = db.execute('SELECT item_id,payload FROM vectors WHERE id=?', (v.vector_id,)).fetchone()
                if old and old != (v.item_id, canonical(v)):
                    raise ValueError('immutable_vectors_conflict')
                if not old:
                    db.execute('INSERT INTO vectors VALUES (?,?,?)', (v.vector_id, v.item_id, canonical(v)))
                    changed = True
            changed |= self._set_clusters(db, clusters)
            if changed:
                self._bump(db, generation)
            return dict(changed=changed, generation=generation + int(changed))

    def generation(self):
        """Read only the committed generation, without materializing vectors."""
        with self._transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
            if row is None:
                raise ValueError('store_not_initialized')
            return int(row[0])

    def snapshot(self):
        with self._transaction() as db:
            config, generation = self._config(db)
            items = [SearchItem.model_validate_json(r[0]) for r in db.execute('SELECT payload FROM items WHERE active=1 ORDER BY id')]
            vectors = [VectorRecord.model_validate_json(r[0]) for r in db.execute('SELECT v.payload FROM vectors v JOIN items i ON i.id=v.item_id WHERE i.active=1 ORDER BY v.id')]
            return IndexSnapshot(**config, generation=generation, items=items, vectors=vectors)

    def get_sources(self, revision_ids):
        with self._transaction() as db:
            out = []
            for id_ in sorted(set(revision_ids)):
                row = db.execute('SELECT payload FROM sources WHERE id=?', (id_,)).fetchone()
                if row is None:
                    raise ValueError('unknown_source_revision:' + id_)
                out.append(ArticleRevision.model_validate_json(row[0]))
            return out

    def get_item(self, item_id, include_inactive=False):
        with self._transaction() as db:
            row = db.execute('SELECT payload,active FROM items WHERE id=?', (item_id,)).fetchone()
            return SearchItem.model_validate_json(row[0]) if row and (row[1] or include_inactive) else None

    @staticmethod
    def _assert_merge_configuration(db, fingerprint):
        for (payload,) in db.execute('SELECT payload FROM clusters'):
            cluster = ClusterSnapshot.model_validate_json(payload)
            if cluster.status != 'resolved':
                continue
            receipt = db.execute('SELECT payload FROM applications WHERE id=?', (cluster.result_id,)).fetchone()
            if receipt is None:
                raise ValueError('resolved_cluster_missing_application')
            if core.digest(json.loads(receipt[0])['configuration']) != fingerprint:
                raise ValueError('merge_configuration_changed_rebuild_index_explicitly')

    def assert_merge_configuration(self, fingerprint):
        """Reject mixing profiles while any resolved representation remains live."""
        with self._transaction() as db:
            self._assert_merge_configuration(db, fingerprint)

    def get_applied_configuration(self, result_id):
        """Return the immutable merge profile, including for unchanged results."""
        with self._transaction() as db:
            row = db.execute('SELECT payload FROM applications WHERE id=?', (result_id,)).fetchone()
            return json.loads(row[0])['configuration'] if row else None

    def get_cluster_snapshot(self, cluster_id):
        with self._transaction() as db:
            row = db.execute('SELECT payload FROM clusters WHERE id=?', (cluster_id,)).fetchone()
            if not row:
                raise KeyError(cluster_id)
            return ClusterSnapshot.model_validate_json(row[0])

    def clusters_for_item(self, item_id):
        with self._transaction() as db:
            row = db.execute('SELECT payload FROM items WHERE id=?', (item_id,)).fetchone()
            if not row:
                return []
            sources = sorted(set(SearchItem.model_validate_json(row[0]).source_revision_ids))
            found = {}
            for offset in range(0, len(sources), 500):
                batch = sources[offset:offset + 500]
                placeholders = ','.join('?' for _ in batch)
                for id_, payload in db.execute(
                    f'SELECT DISTINCT c.id,c.payload FROM clusters c JOIN cluster_members m ON m.cluster_id=c.id WHERE m.revision_id IN ({placeholders})', batch):
                    found[id_] = payload
            return [ClusterSnapshot.model_validate_json(found[id_]) for id_ in sorted(found)]

    @staticmethod
    def _cluster_identity(c):
        return canonical(dict(member_revision_ids=c.member_revision_ids, provenance=c.provenance))

    def _set_clusters(self, db, clusters):
        if len({c.cluster_id for c in clusters}) != len(clusters):
            raise ValueError('duplicate_cluster_id')
        sources = {r[0] for r in db.execute('SELECT id FROM sources')}
        old = {r[0]: ClusterSnapshot.model_validate_json(r[1]) for r in db.execute('SELECT id,payload FROM clusters')}
        counts = Counter(x for c in clusters for x in c.member_revision_ids)
        new = {}
        for c in clusters:
            if not set(c.member_revision_ids) <= sources:
                raise ValueError('unknown_cluster_member')
            identity = self._cluster_identity(c)
            historical = db.execute('SELECT identity FROM cluster_history WHERE id=? AND revision=?', (c.cluster_id, c.revision)).fetchone()
            if historical and historical[0] != identity:
                raise ValueError('changed_cluster_same_revision')
            db.execute('INSERT OR IGNORE INTO cluster_history VALUES (?,?,?)', (c.cluster_id, c.revision, identity))
            ambiguous = bool(c.provenance.get('incomplete_membership')) or any(counts[x] > 1 for x in c.member_revision_ids)
            prev = old.get(c.cluster_id)
            preserve = prev and prev.revision == c.revision and self._cluster_identity(prev) == identity and (prev.status == 'ambiguous') == ambiguous
            new[c.cluster_id] = prev if preserve else c.model_copy(update={'status': 'ambiguous' if ambiguous else 'pending', 'result_id': None})
        affected = set()
        for id_ in set(old) | set(new):
            if old.get(id_) != new.get(id_):
                if id_ in old:
                    affected.update(old[id_].member_revision_ids)
                if id_ in new:
                    affected.update(new[id_].member_revision_ids)
        if not affected:
            return False
        # Every active derived card touching a changed membership is invalidated;
        # restore all its sources, including members outside the new cluster.
        for id_, payload, original, active in db.execute('SELECT id,payload,original,active FROM items').fetchall():
            item = SearchItem.model_validate_json(payload)
            if not original and active and affected.intersection(item.source_revision_ids):
                affected.update(item.source_revision_ids)
                db.execute('UPDATE items SET active=0 WHERE id=?', (id_,))
        for id_, payload in db.execute('SELECT id,payload FROM items WHERE original=1').fetchall():
            if affected.intersection(SearchItem.model_validate_json(payload).source_revision_ids):
                db.execute('UPDATE items SET active=1 WHERE id=?', (id_,))
        db.execute('DELETE FROM clusters')
        db.executemany('INSERT INTO clusters VALUES (?,?)', [(id_, canonical(c)) for id_, c in new.items()])
        self._write_cluster_members(db, new.values())
        return True

    def set_clusters(self, clusters):
        clusters = [_validate(ClusterSnapshot, c) for c in clusters]
        with self._transaction(True) as db:
            _, generation = self._config(db)
            changed = self._set_clusters(db, clusters)
            if changed:
                self._bump(db, generation)
            return dict(changed=changed, generation=generation + int(changed))

    def apply_merge(self, expected, result, encoded_items, *, request):
        expected = _validate(ClusterSnapshot, expected)
        result = _validate(MergeResult, result)
        request = _validate(MergeRequest, request)
        encoded_items = [_validate(EncodedItem, e) for e in encoded_items]
        report = core.validate_merge_result(result, request)
        if not report.valid:
            raise ValueError('invalid_merge_result:' + ','.join(report.errors))
        if result.status != 'completed':
            raise ValueError('merge_not_completed')
        if (request.cluster_id, request.revision) != (expected.cluster_id, expected.revision):
            raise ValueError('request_cluster_mismatch')
        if sorted(a.id for a in request.articles) != expected.member_revision_ids:
            raise ValueError('request_membership_mismatch')
        if result.configuration != core.configuration(request):
            raise ValueError('configuration_mismatch')
        if result.id != core.digest(result.model_dump(exclude={'id', 'cache_hit'})):
            raise ValueError('result_id_mismatch')
        if result.mode == 'unchanged':
            if encoded_items:
                raise ValueError('unchanged_must_keep_originals')
        else:
            if len(encoded_items) != len(result.events) or {e.item.item_id for e in encoded_items} != {e.id for e in result.events}:
                raise ValueError('encoded_event_set_mismatch')
            eventmap = {e.id: e for e in result.events}
            for encoded in encoded_items:
                item, event = encoded.item, eventmap[encoded.item.item_id]
                derived = dict(operation_id=result.operation_id, result_id=result.id,
                               cluster_id=expected.cluster_id, cluster_revision=expected.revision,
                               configuration_fingerprint=core.digest(result.configuration), event=event.model_dump())
                if (item.kind != 'event' or item.title != event.title or item.text != event.text
                    or item.embedding_text != event.embedding_text or item.source_revision_ids != event.article_ids
                    or item.was_merged != event.was_merged or item.publication_count != event.publication_count
                    or item.source_count != event.source_count or item.derived_from != derived):
                    raise ValueError('encoded_event_mismatch')
        payload = canonical(result.model_dump(exclude={'cache_hit'}))
        with self._transaction(True) as db:
            config, generation = self._config(db)
            self._assert_merge_configuration(db, core.digest(result.configuration))
            def stale(reason):
                return ApplyResult(stale=True, generation=generation, reason=reason)
            row = db.execute('SELECT payload FROM clusters WHERE id=?', (expected.cluster_id,)).fetchone()
            if not row:
                return stale('cluster_missing')
            live = ClusterSnapshot.model_validate_json(row[0])
            if (live.revision != expected.revision or live.membership_hash != expected.membership_hash
                or live.member_revision_ids != expected.member_revision_ids or live.provenance != expected.provenance
                or live.status == 'ambiguous'):
                return stale('cluster_changed')
            # Verify exact canonical originals even on the idempotent path.
            for article in request.articles:
                source = db.execute('SELECT payload FROM sources WHERE id=?', (article.id,)).fetchone()
                if not source:
                    return stale('source_missing')
                a = ArticleRevision.model_validate_json(source[0])
                expected_article = Article(id=a.revision_id, title=a.title if a.title_origin == 'source' else '',
                                           text=a.text, published_at=a.published_at, source=a.source or None, url=a.url)
                if article != expected_article:
                    raise ValueError('request_source_mismatch')
            receipt = db.execute('SELECT payload,item_ids FROM applications WHERE id=?', (result.id,)).fetchone()
            if receipt:
                if receipt[0] != payload:
                    raise ValueError('immutable_application_conflict')
                if live.status != 'resolved' or live.result_id != result.id:
                    return stale('application_invalidated')
                ids = json.loads(receipt[1])
                if any(not db.execute('SELECT 1 FROM items WHERE id=? AND active=1', (id_,)).fetchone() for id_ in ids):
                    return stale('representations_changed')
                return ApplyResult(already_applied=True, generation=generation, item_ids=ids)
            if live.status != 'pending':
                return stale('cluster_not_pending')
            members = set(live.member_revision_ids)
            originals = []
            represented = set()
            for id_, item_payload, active in db.execute('SELECT id,payload,active FROM items WHERE original=1'):
                item = SearchItem.model_validate_json(item_payload)
                if members.intersection(item.source_revision_ids):
                    if not active or not set(item.source_revision_ids) <= members:
                        return stale('original_representations_changed')
                    originals.append(id_)
                    represented.update(item.source_revision_ids)
            if represented != members:
                return stale('original_representations_missing')
            for (item_payload,) in db.execute('SELECT payload FROM items WHERE original=0 AND active=1'):
                if members.intersection(SearchItem.model_validate_json(item_payload).source_revision_ids):
                    return stale('derived_representations_changed')
            items = [e.item for e in encoded_items]
            vectors = [v for e in encoded_items for v in e.vectors]
            self._validate_items(items, vectors, members, config['dimension'])
            for encoded in encoded_items:
                if any(v.item_id != encoded.item.item_id for v in encoded.vectors):
                    raise ValueError('encoded_vector_owner_mismatch')
            # Validate ALL immutable IDs before modifying the active set.
            for item in items:
                row = db.execute('SELECT payload,original FROM items WHERE id=?', (item.item_id,)).fetchone()
                if row and (row[0] != canonical(item) or row[1]):
                    raise ValueError('immutable_items_conflict')
            for v in vectors:
                row = db.execute('SELECT item_id,payload FROM vectors WHERE id=?', (v.vector_id,)).fetchone()
                if row and row != (v.item_id, canonical(v)):
                    raise ValueError('immutable_vectors_conflict')
            ids = sorted(originals) if result.mode == 'unchanged' else sorted(i.item_id for i in items)
            if result.mode != 'unchanged':
                db.executemany('UPDATE items SET active=0 WHERE id=?', [(id_,) for id_ in originals])
                for item in items:
                    self._insert_immutable(db, 'items', item.item_id, canonical(item), (1, 0))
                    db.execute('UPDATE items SET active=1 WHERE id=?', (item.item_id,))
                for v in vectors:
                    db.execute('INSERT OR IGNORE INTO vectors VALUES (?,?,?)', (v.vector_id, v.item_id, canonical(v)))
            live = live.model_copy(update={'status': 'resolved', 'result_id': result.id})
            db.execute('UPDATE clusters SET payload=? WHERE id=?', (canonical(live), live.cluster_id))
            db.execute('INSERT INTO applications VALUES (?,?,?,?,?)', (result.id, live.cluster_id, live.revision, payload, canonical(ids)))
            self._bump(db, generation)
            return ApplyResult(applied=True, generation=generation + 1, item_ids=ids)
