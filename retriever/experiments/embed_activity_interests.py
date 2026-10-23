"""Encode separate activity interests with the pinned raw-news BGE profile."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import resource
import time

import numpy as np

from .activity_interest_catalogue import ROOT as CATALOGUE, read, sha, validate

DEFAULT_OUTPUT = Path('data/processed/retriever/activity-interests-v1/embeddings-bge-m3')
DEFAULT_NEWS = Path('data/processed/company_news/run-20261023/bge-index.json')


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def source(catalogue, news_index):
    manifest = read(catalogue / 'manifest.json')
    for name, digest in manifest['files'].items():
        if sha(catalogue / name) != digest:
            raise ValueError(f'catalogue_hash_mismatch:{name}')
    rows = read(catalogue / 'catalogue.json')
    validate(rows, read(catalogue / 'activities.json'))
    news = read(news_index)
    statuses = {a['okved_code']: a['status'] for r in read(catalogue / 'reviews.json') for a in r['activities']}
    topics = []
    for activity in rows:
        for topic in activity['interests']:
            topics.append(dict(row=len(topics), interest_id=activity['okved_code'] + '/' + topic['slug'],
                okved_code=activity['okved_code'], activity=activity['activity'], **topic,
                review_status=statuses[activity['okved_code']]))
    identity = dict(schema_version='activity-interest-embeddings.v1',
        catalogue_sha256=sha(catalogue / 'catalogue.json'), catalogue_manifest_sha256=sha(catalogue / 'manifest.json'),
        news_index_sha256=sha(news_index), encoder_fingerprint=news['fingerprint'],
        input_kind='query', text_policy='exact-description-utf8.v1',
        composition='description_only; no title, code, condition or activity prefix',
        dimension=1024, dtype='float32', rows=len(topics))
    return topics, identity, news


def verify(output, catalogue, news_index):
    topics, identity, _ = source(catalogue, news_index)
    manifest = read(output / 'manifest.json')
    if manifest['status'] != 'complete' or manifest['identity'] != identity:
        raise ValueError('embedding_manifest_or_source_mismatch')
    for name, digest in manifest['files'].items():
        if sha(output / name) != digest:
            raise ValueError(f'embedding_file_hash_mismatch:{name}')
    matrix = np.load(output / 'interest-vectors.npy', allow_pickle=False)
    if matrix.dtype != np.float32 or matrix.shape != (len(topics), 1024) or not np.isfinite(matrix).all():
        raise ValueError('invalid_embedding_matrix')
    norms = np.linalg.norm(matrix, axis=1)
    if not np.allclose(norms, 1., atol=1e-5, rtol=0):
        raise ValueError('embedding_not_normalized')
    mappings = read(output / 'interests.json')
    if len(mappings) != len(topics):
        raise ValueError('mapping_length_mismatch')
    for expected, actual, vector in zip(topics, mappings, matrix):
        for name, value in expected.items():
            if actual.get(name) != value:
                raise ValueError(f'interest_mapping_mismatch:{expected["interest_id"]}:{name}')
        if actual['description_sha256'] != hashlib.sha256(expected['description'].encode()).hexdigest():
            raise ValueError('description_hash_mismatch')
        if actual['vector_sha256'] != hashlib.sha256(vector.astype('<f4', copy=False).tobytes()).hexdigest():
            raise ValueError('row_vector_hash_mismatch')
    return dict(status='verified', interest_count=len(topics), dimension=1024,
                max_norm_error=float(np.max(np.abs(norms - 1.))),
                encoder_fingerprint=identity['encoder_fingerprint'], gpu_required=False)


def build(output, catalogue, news_index):
    began = time.perf_counter()
    topics, identity, news = source(catalogue, news_index)
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'manifest.json').exists():
        return dict(replay_no_inference=True, **verify(output, catalogue, news_index))
    frozen = output / 'build-identity.json'
    if frozen.exists() and read(frozen) != identity:
        raise ValueError('resume_source_changed_use_a_new_output_directory')
    write_json(frozen, identity)
    from retriever.raw_encoder import BGEEncoder
    from retriever.embedding_cache import exact_vector_key
    from llm.event_pipeline.company_news.contracts import fingerprint
    if fingerprint(news['profile']) != news['fingerprint']:
        raise ValueError('news_profile_fingerprint_corrupt')
    encoder = BGEEncoder(output / 'cache.sqlite3', batch_size=8)
    try:
        if encoder.fingerprint != news['fingerprint'] or encoder.fingerprint_metadata != news['profile']:
            raise ValueError('encoder_profile_differs_from_existing_news')
        texts = [t['description'] for t in topics]
        lengths = encoder.token_lengths(texts, input_kind='query')
        if any(n > encoder.profile['max_tokens'] for n in lengths):
            raise ValueError('topic_exceeds_token_limit_no_truncation')
        print(json.dumps(dict(stage='encoder_loaded', count=len(texts), max_tokens=max(lengths),
                             fingerprint=encoder.fingerprint)), flush=True)
        with encoder.cache.writer_lock():
            blocks = []
            for start in range(0, len(texts), 128):
                blocks.append(encoder.encode_queries(texts[start:start + 128]))
                print(json.dumps(dict(stage='embedding', done=min(start + 128, len(texts)), total=len(texts))), flush=True)
            matrix = np.concatenate(blocks)
            topic_metrics = dict(encoder.metrics)
            # Re-encode eight stored queries to check numerical compatibility with
            # the existing news experiment. No news texts are copied to outputs.
            old_queries = np.load(news_index.with_name('bge-queries.npy'), allow_pickle=False)
            if old_queries.shape != (len(news['query_texts']), 1024) or old_queries.dtype != np.float32:
                raise ValueError('invalid_reference_query_matrix')
            indices = list(range(min(8, len(old_queries))))
            if not indices:
                raise ValueError('missing_reference_queries_for_parity_check')
            observed = encoder.encode_queries([news['query_texts'][i] for i in indices])
            reference = old_queries[indices]
            cosine = np.sum(observed * reference, axis=1) / (np.linalg.norm(observed, axis=1) * np.linalg.norm(reference, axis=1))
            max_error = float(np.max(np.abs(observed - reference)))
            parity = dict(reference_query_count=len(indices), minimum_cosine=float(cosine.min()),
                          max_absolute_difference=max_error, reference_matrix_sha256=sha(news_index.with_name('bge-queries.npy')),
                          profile_equal=True, passed=bool(cosine.min() >= .99999 and max_error <= 1e-5))
            if not parity['passed']:
                write_json(output / 'failed-parity.json', parity)
                raise ValueError('reference_query_parity_failed')
            records = []
            for topic, vector, length in zip(topics, matrix, lengths):
                records.append(dict(topic, token_count=length,
                    description_sha256=hashlib.sha256(topic['description'].encode()).hexdigest(),
                    vector_key=exact_vector_key(encoder.fingerprint, topic['description'], input_kind='query'),
                    vector_sha256=hashlib.sha256(vector.astype('<f4', copy=False).tobytes()).hexdigest()))
            with (output / 'interest-vectors.npy.tmp').open('wb') as stream:
                np.save(stream, matrix, allow_pickle=False)
            (output / 'interest-vectors.npy.tmp').replace(output / 'interest-vectors.npy')
            write_json(output / 'interests.json', records)
            write_json(output / 'encoder.json', encoder.fingerprint_metadata)
            write_json(output / 'parity.json', parity)
    finally:
        encoder.cache.close()
    cache_bytes = sum(p.stat().st_size for p in output.glob('cache.sqlite3*') if p.is_file())
    metrics = dict(topics=topic_metrics, including_parity=dict(encoder.metrics),
        elapsed_seconds=time.perf_counter() - began,
        process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        gpu_name=__import__('torch').cuda.get_device_name(0),
        topic_inference_vectors_per_second=(topic_metrics['inferred'] / topic_metrics['inference_seconds']
            if topic_metrics['inference_seconds'] else None),
        cache_bytes=cache_bytes, vector_payload_bytes=int(matrix.nbytes),
        vector_file_bytes=(output / 'interest-vectors.npy').stat().st_size,
        min_tokens=min(lengths), max_tokens=max(lengths))
    write_json(output / 'metrics.json', metrics)
    names = ['build-identity.json', 'interest-vectors.npy', 'interests.json', 'encoder.json', 'parity.json', 'metrics.json']
    manifest = dict(status='complete', identity=identity, files={name: sha(output / name) for name in names},
        assembly_code_sha256=sha(Path(__file__)),
        limitations=['Agent-authored hypotheses, not verified company exposures',
                      'Conditional topics retain applicability conditions; no automatic activation',
                      'All topics encoded, including activities flagged for human scope review',
                      'No company mappings, payment graph embeddings or retrieval evaluation in this step'])
    write_json(output / 'manifest.json', manifest)
    return dict(replay_no_inference=False, metrics=metrics, parity=parity, **verify(output, catalogue, news_index))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--catalogue', type=Path, default=CATALOGUE)
    parser.add_argument('--news-index', type=Path, default=DEFAULT_NEWS)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    result = (verify if args.verify_only else build)(args.output, args.catalogue, args.news_index)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
