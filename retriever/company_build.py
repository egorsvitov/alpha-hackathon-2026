"""Resumable content-addressed company mappings and atomic publication."""
from dataclasses import replace
from contextlib import ExitStack, closing
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import time
from ranking.profiles import file_hash
from .representation_contracts import (BuildReport, CompanyNotFound, CorruptVector, EncoderMismatch,
    ResumeMismatch, FeatureRef, DirectionCoverage, EncodedView, EncodedGraphView, VerificationReport, content_hash)
from .embedding_cache import vector_key, vector_sha256
from .graph_embeddings import aggregate_graph_views


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sync(path):
    with Path(path).open('rb') as stream:
        os.fsync(stream.fileno())


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _connect(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('CREATE TABLE IF NOT EXISTS config (value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, metadata TEXT NOT NULL); CREATE TABLE IF NOT EXISTS views (company TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(company,kind)); CREATE TABLE IF NOT EXISTS errors (company TEXT, detail TEXT);')
    return db


def _hydrate(data, cache):
    data = dict(data)
    data['feature_refs'] = [FeatureRef(**r) for r in data['feature_refs']]
    data['vector'] = cache.get(data['vector_key'])
    if data['vector'] is None or vector_sha256(data['vector']) != data['vector_sha256']:
        raise CorruptVector('missing/corrupt mapped vector')
    metadata = cache.metadata(data['vector_key'])
    if metadata is None or metadata.get('encoder_fingerprint') != data['encoder_fingerprint']:
        raise EncoderMismatch('object fingerprint differs from mapping')
    if data['kind'].startswith('graph_'):
        data['coverage'] = DirectionCoverage(**data['coverage'])
        return EncodedGraphView(**data)
    return EncodedView(**data)


class CompanyEmbeddingSnapshot:
    @classmethod
    def open(cls, path, *, cache):
        path = Path(path)
        if (path / 'CURRENT').exists():
            path = path / 'snapshots' / (path / 'CURRENT').read_text().strip()
        obj = cls()
        obj.path, obj.cache = path, cache
        try:
            obj.manifest = json.loads((path / 'manifest.json').read_text())
            if obj.manifest['status'] != 'complete':
                raise CorruptVector('unpublished snapshot')
            if file_hash(path / 'mapping.sqlite') != obj.manifest['mapping_sha256']:
                raise CorruptVector('mapping checksum mismatch')
            obj.connection = sqlite3.connect('file:' + str((path / 'mapping.sqlite').resolve()) + '?mode=ro', uri=True)
            obj.snapshot_id = obj.manifest['snapshot_id']
            obj.fingerprint = obj.manifest['encoder_fingerprint']
            obj.encoder_fingerprint = obj.fingerprint
        except (OSError, KeyError, json.JSONDecodeError, sqlite3.Error) as exc:
            raise CorruptVector('invalid published snapshot') from exc
        verification = obj.verify()
        if not verification.valid:
            raise CorruptVector('snapshot verification failed', context={'errors': verification.errors})
        return obj

    def company_metadata(self, company_id):
        row = self.connection.execute('SELECT metadata FROM companies WHERE id=?', (company_id,)).fetchone()
        if row is None or not json.loads(row[0]).get('target', True):
            raise CompanyNotFound(company_id)
        return json.loads(row[0])

    def views(self, company_id, *, enabled_views=('business', 'finance', 'trade_geo')):
        self.company_metadata(company_id)
        rows = dict(self.connection.execute('SELECT kind,record FROM views WHERE company=?', (company_id,)))
        return [_hydrate(json.loads(rows[k]), self.cache) for k in enabled_views if k in rows]

    def get_business(self, company_id):
        row = self.connection.execute("SELECT record FROM views WHERE company=? AND kind='business'", (company_id,)).fetchone()
        return _hydrate(json.loads(row[0]), self.cache) if row else None

    def verify(self):
        errors, keys = [], {}
        try:
            for company, kind, raw in self.connection.execute('SELECT company,kind,record FROM views ORDER BY company,kind'):
                record = json.loads(raw)
                if record['company_id'] != company or record['kind'] != kind or record['encoder_fingerprint'] != self.fingerprint:
                    raise CorruptVector('mapping identity mismatch')
                if self.connection.execute('SELECT 1 FROM companies WHERE id=?', (company,)).fetchone() is None:
                    raise CorruptVector('orphan mapping')
                item = _hydrate(record, self.cache)
                keys[item.vector_key] = item.vector_sha256
            if keys != self.manifest['objects']:
                raise CorruptVector('manifest object set mismatch')
            if _semantic_digest(self.connection, self.manifest['build_identity']) != self.snapshot_id:
                raise CorruptVector('semantic snapshot hash mismatch')
        except Exception as exc:
            errors.append(str(exc))
        return VerificationReport(not errors, errors, {'objects': len(keys)})

    def close(self):
        self.connection.close()


class _WorkLookup:
    def __init__(self, db, cache):
        self.db, self.cache = db, cache
    def get_business(self, company_id):
        row = self.db.execute("SELECT record FROM views WHERE company=? AND kind='business'", (company_id,)).fetchone()
        return _hydrate(json.loads(row[0]), self.cache) if row else None


def _semantic_value(value):
    if isinstance(value, dict):
        return {k: _semantic_value(v) for k, v in value.items() if k != 'imported_at'}
    if isinstance(value, list):
        return [_semantic_value(v) for v in value]
    return value


def _semantic_digest(db, identity):
    # Stream logical records: publication must not materialize all view metadata.
    digest = hashlib.sha256(_json(_semantic_value(identity)).encode())
    for table, query in [('companies', 'SELECT id,metadata FROM companies ORDER BY id'),
                         ('views', 'SELECT company,kind,record FROM views ORDER BY company,kind')]:
        digest.update(('\n' + table + '\n').encode())
        for row in db.execute(query):
            digest.update((_json([*row[:-1], _semantic_value(json.loads(row[-1]))]) + '\n').encode())
    return digest.hexdigest()


def build_company_snapshot(repo, *, encoder, cache, graph_store=None, config, company_ids=None, resume=False, fault_hook=None):
    from .company_views import build_company_views, build_universal_view, _templates
    started = time.perf_counter()
    root = Path(config.output_root)
    root.mkdir(parents=True, exist_ok=True)
    ids = sorted(set(company_ids if company_ids is not None else config.scope_ids if config.scope_ids is not None else repo.company_ids) - set(getattr(repo, 'conflicts', {})))
    # Source/policy/model configuration is frozen separately from telemetry.
    identity = dict(config=config.to_dict(), company_ids=ids, encoder_fingerprint=encoder.fingerprint,
        company_source_sha256=repo.sha256, normalization_config_hash=content_hash(repo.config), view_template_version=_templates(config.view_policy)[1],
        payment_manifest=graph_store.dataset_manifest.to_dict() if graph_store is not None and config.graph_policy.enabled else None)
    identity['config']['encoder_config']['model_dir'] = None
    identity['config']['output_root'] = None
    target_ids = set(ids)
    known_ids = repo.company_ids - repo.conflicts
    build_id = content_hash(identity)
    workdir = root / 'work'
    workdir.mkdir(exist_ok=True)
    workpath = workdir / (build_id + '.sqlite')
    def fault(stage):
        if fault_hook:
            fault_hook(stage)
    stats = dict(encoded_texts=0, cache_hits=0, target_companies=len(ids), dependency_only_companies=0, token_count=0)
    with cache.writer_lock(), ExitStack() as resources:
        if resume and not workpath.exists():
            raise ResumeMismatch('no work build matches frozen source/config/scope')
        db = resources.enter_context(closing(_connect(workpath)))
        row = db.execute('SELECT value FROM config').fetchone()
        if row and json.loads(row[0]) != identity:
            raise ResumeMismatch('build identity differs')
        if row is None:
            with db:
                db.execute('INSERT INTO config VALUES (?)', (_json(identity),))
        neighborhoods, dependencies = {}, set()
        if config.graph_policy.enabled:
            if graph_store is None:
                raise ValueError('graph policy requires graph_store')
            for cid in ids:
                neighborhood = graph_store.neighborhood(cid, repo=repo)
                neighborhoods[cid] = neighborhood
                for direction in config.graph_policy.directions:
                    dependencies.update(n.company_id for n in getattr(neighborhood, direction) if n.profile_status == 'known' or n.profile_status == 'usable')
                    # Repository presence is authoritative; A may use finer status labels.
                    dependencies.update(n.company_id for n in getattr(neighborhood, direction) if n.company_id in known_ids)
        stats['dependency_only_companies'] = len(dependencies - set(ids))
        all_ids = ids + sorted(dependencies - set(ids))
        def encode_view(view):
            key = vector_key(encoder.fingerprint, view.text)
            vector = cache.get(key)
            metadata = dict(encoder_fingerprint=encoder.fingerprint, input_kind='query', normalized_text_hash=view.text_hash)
            if vector is None:
                stats['token_count'] += sum(encoder.token_lengths([view.text], input_kind='query'))
                vector = encoder.encode_queries([view.text])[0]
                stats['encoded_texts'] += 1
                fault('before_vector_commit')
                cache.put(key, vector, metadata=metadata)
                fault('after_object_commit')
            else:
                if cache.metadata(key) != metadata:
                    raise EncoderMismatch('cached text metadata mismatch')
                stats['cache_hits'] += 1
            return EncodedView(view.company_id, view.kind, view.content_hash, key, encoder.fingerprint,
                vector_sha256(vector), view.profile_version, view.feature_refs, view.to_dict())
        for start in range(0, len(all_ids), config.company_chunk_size):
            pending = []
            for cid in all_ids[start:start + config.company_chunk_size]:
                if db.execute('SELECT 1 FROM companies WHERE id=?', (cid,)).fetchone():
                    continue
                try:
                    result = build_company_views(repo.get(cid), policy=config.view_policy)
                    selected = list(result.views.values()) if cid in target_ids else [result.views['business']] if 'business' in result.views else []
                    if config.include_universal and cid in target_ids:
                        universal = build_universal_view(result, policy=config.view_policy)
                        if universal is not None:
                            selected.append(universal)
                    records = [encode_view(v) for v in selected]
                    meta = dict(profile_version=result.profile_version, omitted=result.omitted, target=cid in target_ids,
                        temporal_mode=config.temporal_mode, limitations=['profile_data_as_of_unknown'], payment_context_ref=None)
                    pending.append((cid, meta, records))
                except Exception as exc:
                    with db:
                        db.execute('INSERT INTO errors VALUES (?,?)', (cid, str(exc)))
                    db.close()
                    raise
            fault('before_mapping_commit')
            with db:
                for cid, meta, records in pending:
                    db.execute('INSERT INTO companies VALUES (?,?)', (cid, _json(meta)))
                    for item in records:
                        db.execute('INSERT INTO views VALUES (?,?,?)', (cid, item.kind, _json(item.to_dict())))
                fault('before_checkpoint')
            fault('after_checkpoint')
        lookup = _WorkLookup(db, cache)
        if config.graph_policy.enabled:
            for cid in ids:
                result = aggregate_graph_views(neighborhoods[cid], lookup=lookup, policy=config.graph_policy)
                records = []
                for item in result.views.values():
                    item = replace(item, profile_version=json.loads(db.execute('SELECT metadata FROM companies WHERE id=?', (cid,)).fetchone()[0])['profile_version'])
                    cache.put(item.vector_key, item.vector, metadata=dict(encoder_fingerprint=encoder.fingerprint, representation_type='aggregation'))
                    records.append(item)
                with db:
                    for item in records:
                        db.execute('INSERT OR REPLACE INTO views VALUES (?,?,?)', (cid, item.kind, _json(item.to_dict())))
                    meta = json.loads(db.execute('SELECT metadata FROM companies WHERE id=?', (cid,)).fetchone()[0])
                    meta['omitted'].update(result.omitted)
                    meta['payment_context_ref'] = dict(neighborhood=neighborhoods[cid].to_dict(), coverage={k:v.to_dict() for k,v in result.coverage.items()})
                    db.execute('UPDATE companies SET metadata=? WHERE id=?', (_json(meta), cid))
        snapshot_id = _semantic_digest(db, identity)
        objects = {}
        for (raw_record,) in db.execute('SELECT record FROM views ORDER BY company,kind'):
            record = json.loads(raw_record)
            _hydrate(record, cache)
            objects[record['vector_key']] = record['vector_sha256']
        snapshots = root / 'snapshots'
        snapshots.mkdir(exist_ok=True)
        target = snapshots / snapshot_id
        temporary = snapshots / ('.' + snapshot_id + '.tmp')
        fault('before_publish')
        if not target.exists():
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.mkdir()
            copied = sqlite3.connect(temporary / 'mapping.sqlite')
            db.backup(copied)
            copied.close()
            manifest = dict(status='complete', snapshot_id=snapshot_id, encoder_fingerprint=encoder.fingerprint,
                build_identity=identity, objects=objects, storage_locator=str(cache.path.resolve()),
                mapping_sha256=file_hash(temporary / 'mapping.sqlite'))
            (temporary / 'manifest.json').write_text(_json(manifest) + '\n')
            _sync(temporary / 'mapping.sqlite')
            _sync(temporary / 'manifest.json')
            _sync_dir(temporary)
            candidate = CompanyEmbeddingSnapshot.open(temporary, cache=cache)
            candidate.close()
            temporary.rename(target)
            _sync_dir(snapshots)
        # Existing snapshots must be verified before becoming CURRENT again.
        candidate = CompanyEmbeddingSnapshot.open(target, cache=cache)
        candidate.close()
        fault('after_rename_before_current')
        pointer = root / '.CURRENT.tmp'
        pointer.write_text(snapshot_id + '\n')
        _sync(pointer)
        pointer.replace(root / 'CURRENT')
        _sync_dir(root)
        db.close()
    stats['duration_seconds'] = time.perf_counter() - started
    return BuildReport(snapshot_id, str(target), statistics=stats)
