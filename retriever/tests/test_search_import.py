import hashlib
import json

import numpy as np
import pytest

from retriever.importers import (corpus_sha256, read_articles, read_clusters,
                                 read_interests, read_vector_bundle)
from retriever.models import (ArticleRevision, CompanyInterestSet, IndexSnapshot, Interest,
                              SearchItem, VectorRecord, text_hash)
from retriever.search import exact_search


def item(key, text='whole source text'):
    return SearchItem(item_id=key, text=text, embedding_text=text, source_revision_ids=[key])


def vector(key, values, suffix='', text='whole source text'):
    return VectorRecord(vector_id=key+suffix, item_id=key, embedding_text_sha256=text_hash(text), values=values)


def interests(rows=None):
    return CompanyInterestSet(company_id='company', profile_version='p1', interest_set_version='i1',
                              encoder_fingerprint='fp', dimension=2, interests=rows or [])


def test_exact_matrix_oracle_fragments_ties_and_exclusions():
    rng = np.random.default_rng(4)
    raw = rng.normal(size=(13, 2))
    raw /= np.linalg.norm(raw, axis=1)[:, None]
    vs = [vector(str(i//2), values.tolist(), suffix=f'/{i}') for i, values in enumerate(raw)]
    its = [item(str(i)) for i in range(7)]
    active = [Interest(interest_id='z', text='first', vector=[1, 0]),
              Interest(interest_id='a', text='second', vector=[0, 1])]
    inactive = [Interest(interest_id='disabled', text='x', vector=[-1, 0], active=False),
                Interest(interest_id='graph', text='x', vector=[-1, 0], family='graph')]
    profile = interests(active+inactive)
    snapshot = IndexSnapshot(corpus_id='c', generation=1, encoder_fingerprint='fp', dimension=2, items=its, vectors=vs)
    hits = exact_search(snapshot, profile, block_size=3)
    oracle = np.asarray([a.vector for a in active]) @ raw.T
    expected = {str(i): oracle[:, i*2:i*2+2].max() for i in range(7)}
    assert [h.item_id for h in hits] == sorted(expected, key=lambda k: (-expected[k], k))
    for hit in hits:
        assert hit.score == pytest.approx(expected[hit.item_id])
        assert set(hit.interest_scores) == {'a', 'z'}
    ties = snapshot.model_copy(update={'items': [item('b'), item('a')], 'vectors': [vector('b', [1, 0]), vector('a', [1, 0])]})
    assert [h.item_id for h in exact_search(ties, profile)] == ['a', 'b']
    assert 'graph' in exact_search(snapshot, profile, include_graph=True)[0].interest_scores
    assert exact_search(snapshot, interests()) == []
    with pytest.raises(ValueError, match='fingerprint'):
        exact_search(snapshot, profile.model_copy(update={'encoder_fingerprint': 'other'}))
    with pytest.raises(ValueError, match='dimension'):
        exact_search(snapshot.model_copy(update={'dimension': 3}), profile)
    with pytest.raises(ValueError, match='missing_item_vectors'):
        exact_search(snapshot.model_copy(update={'vectors': []}), profile)


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def test_parser_revisions_metadata_dates_and_long_text(tmp_path):
    raw = dict(article_id='external', source='source', url='https://example.test', title='title',
               published_at='2026-10-24', language='ru', text='a'*5000)
    path = write_json(tmp_path/'articles.json', [raw, dict(raw, text=''), dict(raw, title='edited')])
    rows, diagnostic = read_articles(path)
    assert len(rows) == 2 and rows[0].revision_id != rows[1].revision_id
    assert len(rows[0].text) == 5000
    assert rows[0].publication_precision == 'day'
    assert diagnostic[0]['reason'] == 'empty_text'
    prepared = dict(document_id='prepared-v1', article=raw, provenance=[{'dataset': 'synthetic'}])
    rows, _ = read_articles(write_json(path, [prepared]))
    assert rows[0].revision_id == 'prepared-v1'
    assert rows[0].provenance[0] == {'dataset': 'synthetic'}
    with pytest.raises(ValueError, match='timezone_required'):
        read_articles(write_json(path, [dict(raw, published_at='2026-10-24T12:00:00')]))
    with pytest.raises(ValueError, match='text_hash_mismatch'):
        read_articles(write_json(path, [dict(prepared, text_sha256='bad')]))
    with pytest.raises(ValueError, match='conflicting_immutable_revision'):
        read_articles(write_json(path, [prepared, dict(prepared, article=dict(raw, title='changed'))]))


def bundle_fixture(tmp_path):
    article = ArticleRevision(revision_id='a', article_id='a', text='whole source text')
    manifest = dict(schema_version='retriever-vector-bundle.v1', corpus_id='c',
                    corpus_sha256=corpus_sha256([article]), article_sources={'a': article.text_sha256},
                    encoder_fingerprint='fp', dimension=2, items=[item('a').model_dump()],
                    vectors=[vector('a', [1, 0]).model_dump()])
    path = tmp_path/'manifest.json'
    return article, manifest, path


def test_inline_bundle_requires_full_identity_and_correct_vectors(tmp_path):
    article, manifest, path = bundle_fixture(tmp_path)
    assert read_vector_bundle(write_json(path, manifest), [article])['corpus_id'] == 'c'
    for field, value, error in [('corpus_sha256', 'bad', 'corpus_hash'),
                                ('article_sources', {'a': 'bad'}, 'source_hash'),
                                ('dimension', 3, 'dimension'),
                                ('encoder_fingerprint', '', 'fingerprint')]:
        with pytest.raises(ValueError, match=error):
            read_vector_bundle(write_json(path, dict(manifest, **{field: value})), [article])
    for values, error in [([2, 0], 'unnormalized'), ([float('nan'), 0], 'finite')]:
        row = dict(manifest['vectors'][0], values=values)
        with pytest.raises(ValueError, match=error):
            read_vector_bundle(write_json(path, dict(manifest, vectors=[row])), [article])
    with pytest.raises(ValueError, match='embedding_text_hash'):
        read_vector_bundle(write_json(path, dict(manifest, vectors=[dict(manifest['vectors'][0], embedding_text_sha256='bad')])), [article])
    modified = article.model_copy(update={'title': 'new metadata'})
    with pytest.raises(ValueError, match='corpus_hash'):
        read_vector_bundle(write_json(path, manifest), [modified])


def test_npy_mapping_hashes_and_trusted_fragments(tmp_path):
    article, manifest, path = bundle_fixture(tmp_path)
    np.save(tmp_path/'vectors.npy', np.array([[1., 0.], [0., 1.]], dtype=np.float32))
    mapping = [dict(vector('a', [1, 0], '/1', 'whole').model_dump(exclude={'values'}), embedding_text='whole'),
               dict(vector('a', [0, 1], '/2', 'text').model_dump(exclude={'values'}), embedding_text='text')]
    write_json(tmp_path/'mapping.json', mapping)
    del manifest['vectors']
    manifest.update(matrix_file='vectors.npy', mapping_file='mapping.json',
                    matrix_sha256=hashlib.sha256((tmp_path/'vectors.npy').read_bytes()).hexdigest(),
                    mapping_sha256=hashlib.sha256((tmp_path/'mapping.json').read_bytes()).hexdigest())
    result = read_vector_bundle(write_json(path, manifest), [article])
    assert len(result['vectors']) == 2
    assert result['items'][0].derived_from['fragment_text_hashes'] == sorted([text_hash('whole'), text_hash('text')])
    with pytest.raises(ValueError, match='artifact_hash'):
        read_vector_bundle(write_json(path, dict(manifest, mapping_sha256='bad')), [article])
    mapping[0].update(embedding_text='fabricated', embedding_text_sha256=text_hash('fabricated'))
    write_json(tmp_path/'mapping.json', mapping)
    manifest['mapping_sha256'] = hashlib.sha256((tmp_path/'mapping.json').read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='fragment_not_source_slice'):
        read_vector_bundle(write_json(path, manifest), [article])


def test_cluster_overlap_and_unknown_members(tmp_path):
    articles = [ArticleRevision(revision_id=key, article_id=key, text=key) for key in 'abc']
    clusters = [{'cluster_id': 'one', 'revision': '1', 'member_revision_ids': ['a', 'b']},
                {'cluster_id': 'two', 'revision': '1', 'member_revision_ids': ['b', 'c']}]
    path = write_json(tmp_path/'clusters.json', clusters)
    result, diagnostics = read_clusters(path, articles)
    assert [c.status for c in result] == ['ambiguous', 'ambiguous']
    assert diagnostics[0]['revision_id'] == 'b'
    with pytest.raises(ValueError, match='unknown_cluster_member'):
        read_clusters(path, articles[:2])


def test_interest_native_json(tmp_path):
    profile = interests([Interest(interest_id='q', text='interest', vector=[1, 0])])
    path = write_json(tmp_path/'interests.json', profile.model_dump())
    assert read_interests(path) == profile


def test_prepared_provenance_recovers_day_and_generated_title(tmp_path):
    article = dict(article_id='prepared', title='synthetic identifier', text='Original complete source body.',
                   source='source', url=None, language='ru', published_at=None,
                   content_kind='article', is_full_original_text=True, is_synthetic=False)
    provenance = [{'title_origin': 'generated_identifier', 'publication': {
        'precision': 'day', 'date': '2026-10-23', 'raw': '23 October 2026'}}]
    wrapped = dict(document_id='prepared-immutable', article=article,
                   text_sha256=text_hash(article['text']), article_sha256='opaque-legacy-hash', provenance=provenance)
    path = write_json(tmp_path/'prepared.json', [wrapped])
    result, diagnostics = read_articles(path)
    assert not diagnostics
    assert result[0].title_origin == 'generated_identifier'
    assert result[0].published_at == '2026-10-23'
    assert result[0].publication_precision == 'day'
    assert result[0].provenance[0] == provenance[0]
    # Complete native revisions round-trip without introducing a new provenance layer.
    assert read_articles(write_json(path, [result[0].model_dump()]))[0] == result
