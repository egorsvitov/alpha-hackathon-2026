"""Strict standalone imports. No encoder, legacy NLP, or implicit filtering."""
from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np

from .models import (ArticleRevision, ClusterSnapshot, CompanyInterestSet,
                     SearchItem, VectorRecord, digest, instant, text_hash)


def _rows(path):
    path = Path(path)
    if path.suffix.lower() == '.jsonl':
        with path.open(encoding='utf-8') as stream:
            return [json.loads(line) for line in stream if line.strip()]
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, list):
        return data
    for key in ('articles', 'clusters', 'rows'):
        if key in data:
            return data[key]
    raise ValueError('expected_array_or_jsonl')


def _publication(value, precision=None):
    if value is None or value == '':
        return None, 'unknown'
    if precision == 'unknown':
        return None, 'unknown'
    if precision == 'day' or (precision is None and len(value) == 10):
        date.fromisoformat(value)
        return value, 'day'
    instant(value)  # Do not invent a timezone for naive timestamps.
    return value, 'timestamp'


def read_articles(path) -> tuple[list[ArticleRevision], list[dict]]:
    articles, diagnostics, seen = [], [], {}
    for number, row in enumerate(_rows(path), 1):
        if not isinstance(row, dict):
            raise ValueError(f'article_row_not_object:{number}')
        raw = row.get('article', row.get('raw', row))
        if not isinstance(raw, dict):
            raise ValueError(f'article_payload_not_object:{number}')
        text = raw.get('text')
        if text is not None and not isinstance(text, str):
            raise ValueError(f'article_text_not_string:{number}')
        if text is None or not text.strip():
            diagnostics.append({'row': number, 'article_id': raw.get('article_id'), 'reason': 'empty_text'})
            continue
        provenance = list(row.get('provenance', raw.get('provenance', [])))
        published_value = row.get('published_at', raw.get('published_at'))
        precision_value = row.get('publication_precision', raw.get('publication_precision'))
        title_origin = row.get('title_origin', raw.get('title_origin'))
        origins = {p['title_origin'] for p in provenance if p.get('title_origin') is not None}
        if title_origin is None and len(origins) > 1:
            raise ValueError('conflicting_title_origin_provenance')
        if title_origin is None:
            title_origin = next(iter(origins), 'source')
        if published_value is None:
            days = {p['publication']['date'] for p in provenance
                    if isinstance(p.get('publication'), dict) and p['publication'].get('precision') == 'day'
                    and p['publication'].get('date')}
            if len(days) > 1:
                raise ValueError('conflicting_publication_provenance')
            if days:
                published_value, precision_value = next(iter(days)), 'day'
        published, precision = _publication(published_value, precision_value)
        identity = {key: value for key, value in row.items() if key not in ('revision_id', 'document_id', 'text_sha256')}
        # Metadata is part of the immutable revision identity, including original dates.
        revision_id = row.get('revision_id') or row.get('document_id') or raw.get('revision_id') or raw.get('document_id') or ('article:' + digest(identity))
        if raw is not row:
            provenance.append({'kind': 'prepared_wrapper', 'metadata': {key: value for key, value in row.items() if key not in ('article', 'raw', 'provenance')}})
        if raw.get('published_at') and precision == 'unknown':
            provenance.append({'kind': 'original_publication', 'value': raw['published_at']})
        article = ArticleRevision(
            revision_id=revision_id, article_id=raw['article_id'], text=raw['text'],
            text_sha256=row.get('text_sha256', raw.get('text_sha256', '')),
            title=raw.get('title', ''), title_origin=title_origin,
            source=raw.get('source', ''), url=raw.get('url'), language=raw.get('language', 'und'),
            published_at=published, publication_precision=precision,
            observed_at=row.get('observed_at', raw.get('observed_at')), provenance=provenance)
        if article.revision_id in seen:
            if seen[article.revision_id] != article:
                raise ValueError('conflicting_immutable_revision')
            diagnostics.append({'row': number, 'revision_id': article.revision_id, 'reason': 'duplicate_revision'})
            continue
        seen[article.revision_id] = article
        articles.append(article)
    return articles, diagnostics


def read_clusters(path, articles) -> tuple[list[ClusterSnapshot], list[dict]]:
    known = {a.revision_id for a in articles}
    clusters = [ClusterSnapshot.model_validate(row) for row in _rows(path)]
    if len({c.cluster_id for c in clusters}) != len(clusters):
        raise ValueError('duplicate_cluster_id')
    for cluster in clusters:
        if not set(cluster.member_revision_ids) <= known:
            raise ValueError('unknown_cluster_member')
    counts = Counter(member for cluster in clusters for member in cluster.member_revision_ids)
    overlap = {member for member, count in counts.items() if count > 1}
    diagnostics = [{'reason': 'overlapping_cluster_membership', 'revision_id': member}
                   for member in sorted(overlap)]
    return [c.model_copy(update={'status': 'ambiguous'}) if overlap.intersection(c.member_revision_ids) else c
            for c in clusters], diagnostics


def corpus_sha256(articles):
    """Digest covers full immutable revisions, including metadata and provenance."""
    return digest([a.model_dump(mode='json') for a in sorted(articles, key=lambda a: a.revision_id)])


def _artifact(root, name, expected_hash):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('artifact_path_outside_bundle')
    if not expected_hash or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise ValueError('artifact_hash_mismatch')
    return path


def read_vector_bundle(path, articles) -> dict:
    path = Path(path)
    if path.is_dir():
        path = path / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 'retriever-vector-bundle.v1':
        raise ValueError('unsupported_vector_bundle_schema')
    if not manifest.get('corpus_id') or manifest.get('corpus_sha256') != corpus_sha256(articles):
        raise ValueError('corpus_hash_mismatch')
    sources = {a.revision_id: a.text_sha256 for a in articles}
    if len(sources) != len(articles) or manifest.get('article_sources') != sources:
        raise ValueError('article_source_hash_mismatch')
    dimension = manifest.get('dimension')
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension < 1:
        raise ValueError('invalid_dimension')
    fingerprint = manifest.get('encoder_fingerprint')
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ValueError('missing_encoder_fingerprint')
    items = [SearchItem.model_validate(row) for row in manifest['items']]
    by_id = {item.item_id: item for item in items}
    if len(by_id) != len(items):
        raise ValueError('duplicate_item_id')
    covered = set()
    source_articles = {a.revision_id: a for a in articles}
    for item in items:
        if not set(item.source_revision_ids) <= sources.keys():
            raise ValueError('unknown_item_source')
        if item.kind == 'article':
            if len(item.source_revision_ids) != 1:
                raise ValueError('article_requires_single_source_revision')
            source = source_articles[item.source_revision_ids[0]]
            if item.text != source.text or item.title != source.title:
                raise ValueError('article_source_content_mismatch')
            allowed_texts = [source.text]
            if source.title_origin == 'source':
                allowed_texts.append(source.title+'\n\n'+source.text)
            if item.embedding_text not in allowed_texts:
                raise ValueError('article_embedding_text_mismatch')
        covered.update(item.source_revision_ids)
    if covered != sources.keys():
        raise ValueError('missing_source_items')
    fragment_texts = manifest.get('fragment_texts', {})
    if 'matrix_file' in manifest:
        if 'vectors' in manifest:
            raise ValueError('ambiguous_vector_representation')
        matrix_path = _artifact(path.parent, manifest['matrix_file'], manifest.get('matrix_sha256'))
        mapping_path = _artifact(path.parent, manifest['mapping_file'], manifest.get('mapping_sha256'))
        matrix = np.load(matrix_path, allow_pickle=False)
        mapping = _rows(mapping_path)
        if matrix.ndim != 2 or matrix.shape != (len(mapping), dimension) or matrix.dtype.kind != 'f':
            raise ValueError('matrix_shape_or_dtype_mismatch')
        rows = []
        for position, record in enumerate(mapping):
            record = dict(record)
            fragment = record.pop('embedding_text', None)
            if fragment is not None:
                fragment_texts[record['vector_id']] = fragment
            if 'values' in record:
                raise ValueError('mapping_must_not_contain_values')
            rows.append(dict(record, values=matrix[position].tolist()))
    else:
        rows = manifest['vectors']
    vectors, seen, represented = [], set(), set()
    source_texts = {a.revision_id: a.text for a in articles}
    trusted_fragments = {}
    fragment_proofs = {}
    for row in rows:
        vector = VectorRecord.model_validate(row)
        if vector.vector_id in seen:
            raise ValueError('duplicate_vector_id')
        seen.add(vector.vector_id)
        item = by_id.get(vector.item_id)
        if item is None or item.item_version != vector.item_version:
            raise ValueError('vector_item_version_mismatch')
        if len(vector.values) != dimension:
            raise ValueError('vector_dimension_mismatch')
        fragment = fragment_texts.get(vector.vector_id)
        if fragment is not None and (not isinstance(fragment, str) or not fragment.strip()):
            raise ValueError('invalid_fragment_text')
        if vector.embedding_text_sha256 != text_hash(fragment if fragment is not None else item.embedding_text):
            raise ValueError('embedding_text_hash_mismatch')
        if fragment is not None:
            if item.kind != 'article':
                raise ValueError('event_fragment_not_supported')
            if not any(fragment in source_texts[source] for source in item.source_revision_ids):
                raise ValueError('fragment_not_source_slice')
            trusted_fragments.setdefault(item.item_id, set()).add(text_hash(fragment))
            fragment_proofs.setdefault(item.item_id, {})[text_hash(fragment)] = fragment
        represented.add(vector.item_id)
        vectors.append(vector)
    if represented != by_id.keys():
        raise ValueError('missing_item_vectors')
    if not fragment_texts.keys() <= seen:
        raise ValueError('unknown_fragment_vector')
    sanitized = []
    for item in items:
        provenance = dict(item.derived_from)
        provenance.pop('fragment_text_hashes', None)
        provenance.pop('fragment_texts', None)
        if item.item_id in trusted_fragments:
            provenance['fragment_text_hashes'] = sorted(trusted_fragments[item.item_id])
            provenance['fragment_texts'] = fragment_proofs[item.item_id]
        sanitized.append(item.model_copy(update={'derived_from': provenance}))
    return dict(items=sanitized, vectors=vectors, encoder_fingerprint=fingerprint,
                dimension=dimension, corpus_id=manifest['corpus_id'])


def read_interests(path) -> CompanyInterestSet:
    return CompanyInterestSet.model_validate_json(Path(path).read_text(encoding='utf-8'))
