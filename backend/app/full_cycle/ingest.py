"""Build a native vector bundle and validate it before atomically publishing an index."""
from pathlib import Path
import json
import os
import time

import numpy as np

from retriever.importers import read_articles, read_clusters, read_vector_bundle, corpus_sha256
from retriever.models import ClusterSnapshot, SearchItem, VectorRecord, canonical, digest, text_hash
from retriever.store import SQLiteSearchStore
from llm.event_pipeline.semantic.core import group_vectors, VERSION as GROUP_VERSION
from .storage import PipelineError, now


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        f.write(canonical(value))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def ingest(workspace, corpus, *, encoder=None, vectors=None, clusters=None, expected_fingerprint=None,
           expected_dimension=None, grouping_threshold=0.75, corpus_id='collector-v1'):
    began = time.monotonic()
    try:
        with workspace.writer():
            articles, diagnostics = read_articles(corpus)
            if not articles:
                raise PipelineError('empty_corpus')
            if any(d['reason'] == 'empty_text' for d in diagnostics):
                raise PipelineError('incomplete_corpus_empty_text')
            if len({a.article_id for a in articles}) != len(articles):
                raise PipelineError('multiple_current_revisions_for_article')
            # Verify immutable IDs before encoding, including across restarts.
            with workspace.db() as db:
                for a in articles:
                    old = db.execute('SELECT payload FROM revisions WHERE id=?', (a.revision_id,)).fetchone()
                    if old and old[0] != canonical(a):
                        raise PipelineError('immutable_revisions_conflict')
            fingerprint = expected_fingerprint or (encoder.fingerprint if encoder else None)
            dimension = expected_dimension or (encoder.dimension if encoder else None)
            if not fingerprint or not dimension:
                raise PipelineError('pinned_encoder_profile_required')
            if encoder and (encoder.fingerprint, encoder.dimension) != (fingerprint, dimension):
                raise PipelineError('encoder_profile_mismatch')
            identity = digest(dict(corpus=corpus_sha256(articles), fingerprint=fingerprint, dimension=dimension,
                                   vectors=text_hash(Path(vectors, 'manifest.json').read_text() if Path(vectors).is_dir() else Path(vectors).read_text()) if vectors else None,
                                   clusters=text_hash(Path(clusters).read_text()) if clusters else None,
                                   grouping=[GROUP_VERSION, grouping_threshold], corpus_id=corpus_id))
            current = workspace.state('index')
            if current and current['identity'] == identity:
                # External bundles still undergo artifact/hash validation on reuse.
                if vectors:
                    bundle = read_vector_bundle(vectors, articles)
                    if (bundle['encoder_fingerprint'], bundle['dimension']) != (fingerprint, dimension):
                        raise PipelineError('encoder_profile_mismatch')
                result = dict(current, changed=False, new_articles=0, seconds=time.monotonic()-began)
                workspace.set_state('ingestion', dict(status='ready', **result))
                return result
            build = workspace.root / 'indexes' / identity
            build.mkdir(parents=True, exist_ok=True)
            new_encoded = 0
            if vectors:
                bundle = read_vector_bundle(vectors, articles)
            else:
                if encoder is None:
                    raise PipelineError('encoder_required_for_new_articles')
                items, records = [], []
                # Cache binds exact text AND fingerprint; normalization is never guessed.
                cache_path = workspace.root / 'embedding-cache.sqlite3'
                import sqlite3
                with sqlite3.connect(cache_path) as cache:
                    cache.execute('CREATE TABLE IF NOT EXISTS vectors(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')
                    texts = [(a.title + '\n\n' if a.title_origin == 'source' and a.title else '') + a.text for a in articles]
                    cached = {}
                    for text in dict.fromkeys(texts):
                        key = digest([fingerprint, dimension, text_hash(text)])
                        row = cache.execute('SELECT payload FROM vectors WHERE key=?', (key,)).fetchone()
                        if row:
                            cached[text] = json.loads(row[0])
                        else:
                            matrix = np.asarray(encoder.encode_event_texts([text]))
                            if matrix.shape != (1, dimension) or matrix.dtype != np.float32:
                                raise PipelineError('invalid_encoded_article_matrix')
                            # VectorRecord enforces unit norm, finite values, and exact hash.
                            value = VectorRecord(vector_id=key, item_id=key,
                                embedding_text_sha256=text_hash(text), values=matrix[0].tolist()).values
                            cache.execute('INSERT INTO vectors VALUES (?,?)', (key, canonical(value)))
                            cached[text] = value
                            new_encoded += 1
                    for a, text in zip(articles, texts):
                        item = SearchItem(item_id=a.revision_id, item_version=digest(a), title=a.title, text=a.text,
                                          embedding_text=text, source_revision_ids=[a.revision_id])
                        vector = VectorRecord(vector_id=digest([item.item_id, item.item_version, fingerprint]),
                            item_id=item.item_id, item_version=item.item_version, embedding_text_sha256=text_hash(text), values=cached[text])
                        items.append(item); records.append(vector)
                native = dict(schema_version='retriever-vector-bundle.v1', corpus_id=corpus_id,
                              corpus_sha256=corpus_sha256(articles), article_sources={a.revision_id:a.text_sha256 for a in articles},
                              encoder_fingerprint=fingerprint, dimension=dimension, items=[i.model_dump(mode='json') for i in items], vectors=[v.model_dump(mode='json') for v in records])
                atomic_json(build / 'manifest.json', native)
                bundle = read_vector_bundle(build, articles)
            if (bundle['encoder_fingerprint'], bundle['dimension'], bundle['corpus_id']) != (fingerprint, dimension, corpus_id):
                raise PipelineError('encoder_profile_or_corpus_mismatch')
            if clusters:
                groups, group_diagnostics = read_clusters(clusters, articles)
                diagnostics.extend(group_diagnostics)
            else:
                # This existing algorithm needs one full vector per original. Fragment-only
                # imports must provide their already versioned reading groups explicitly.
                vector_by_item = {v.item_id:v for v in bundle['vectors']}
                item_by_source = {i.source_revision_ids[0]:i for i in bundle['items']}
                ordered = sorted(articles, key=lambda a:a.revision_id)
                matrix = []
                for a in ordered:
                    item = item_by_source[a.revision_id]
                    v = vector_by_item[item.item_id]
                    if v.embedding_text_sha256 != text_hash(item.embedding_text):
                        raise PipelineError('fragment_bundle_requires_clusters')
                    matrix.append(v.values)
                config = dict(threshold=grouping_threshold, alpha=0.0, backend='scipy' if len(ordered)>512 else 'reference', neighbors=0)
                grouped = group_vectors(matrix, [dict(precision=a.publication_precision,value=a.published_at) for a in ordered], **config)
                groups = []
                for g in grouped['groups']:
                    members = sorted(ordered[i].revision_id for i in g)
                    group_id = 'reading:' + digest(members)
                    groups.append(ClusterSnapshot(cluster_id=group_id, revision=digest([members, config, GROUP_VERSION]),
                        member_revision_ids=members, provenance=dict(algorithm=GROUP_VERSION, config=config, interpretation='reading_group_not_event')))
            index_path = build / 'index.sqlite3'
            with SQLiteSearchStore(index_path) as store:
                store.import_bundle(articles=articles, clusters=groups, **bundle)
                snapshot = store.snapshot()
            # Publishing pointer and immutable archive is one journal transaction.
            with workspace.db() as db:
                added = sum(workspace.immutable(db, 'revisions', (a.revision_id,), a) for a in articles)
                pointer = dict(identity=identity, path=str(index_path), corpus_id=bundle['corpus_id'],
                    encoder_fingerprint=fingerprint, dimension=dimension, article_count=len(articles),
                    group_count=len(groups), vector_count=len(bundle['vectors']), generation=snapshot.generation,
                    applied_at=now(), corpus_sha256=corpus_sha256(articles), new_articles=added,
                    new_embeddings=new_encoded, seconds=time.monotonic()-began, diagnostics=diagnostics)
                workspace._state(db, 'index', pointer)
                workspace._state(db, 'ingestion', dict(status='ready', changed=True, **pointer))
            return dict(pointer, changed=True)
    except Exception as exc:
        code = exc.code if isinstance(exc, PipelineError) else ('ingestion_failed:' + type(exc).__name__)
        workspace.set_state('ingestion', dict(status='failed', code=code, at=now(), last_good_index=bool(workspace.state('index'))))
        raise
