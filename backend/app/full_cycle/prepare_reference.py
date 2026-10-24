"""Explicit converter for the private run-20261023 artifact contract, no model calls.

Unlike import autodetection, this checks named legacy artifact hashes and writes
an ordinary native bundle + complete CompanyInterestSet before the worker runs.
"""
import argparse
import json
from pathlib import Path

import numpy as np

from retriever.cpu_encoder import file_hash
from retriever.importers import corpus_sha256,read_vector_bundle
from retriever.models import ArticleRevision,SearchItem,VectorRecord,CompanyInterestSet,Interest,ClusterSnapshot,digest,text_hash,canonical
from .ingest import atomic_json
from .storage import PipelineError


def rows(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines() if s.strip()]


def recover_url(document, raw_root, cache):
    """Recover only an exact collector original, with file and text hash proofs."""
    if raw_root is None:
        return None
    raw_root = Path(raw_root).resolve()
    found = set()
    for origin in document['provenance']:
        if origin.get('format') != 'collector_jsonl':
            continue
        path = (raw_root / origin['file']).resolve()
        if not path.is_relative_to(raw_root):
            raise PipelineError('source_path_outside_raw_root')
        if path not in cache:
            if file_hash(path) != origin.get('file_sha256'):
                raise PipelineError('original_collector_hash_mismatch')
            cache[path] = rows(path)
        original = cache[path][origin['line'] - 1]
        reference = document['reference']
        if (original['article_id'] != reference['article_id'] or original['text'] != document['text']
            or text_hash(original['text']) != reference['content_hash']):
            raise PipelineError('original_collector_content_mismatch')
        if original.get('url'):
            from urllib.parse import urlsplit
            url = original['url']
            parsed = urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                raise PipelineError('invalid_original_source_url')
            found.add(url)
    if len(found) > 1:
        raise PipelineError('conflicting_original_source_urls')
    return next(iter(found), None)


def prepare(news,topics_root,output,companies,raw_root=None):
    news,topics_root,output=Path(news),Path(topics_root),Path(output)
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    index=json.loads((news/'bge-index.json').read_text())
    meta=json.loads((topics_root/'manifest.json').read_text())
    if digest(index['profile'])!=index['fingerprint'] or index['profile']['profile']['model']!='BAAI/bge-m3':
        raise PipelineError('invalid_reference_encoder_profile')
    if file_hash(news/'bge-index.json')!=meta['identity']['news_index_sha256'] or meta['identity']['encoder_fingerprint']!=index['fingerprint']:
        raise PipelineError('news_interest_reference_mismatch')
    for name,expected in meta['files'].items():
        if not (topics_root/name).resolve().is_relative_to(topics_root.resolve()) or file_hash(topics_root/name)!=expected:
            raise PipelineError('interest_artifact_hash_mismatch')
    wanted={'documents.jsonl','profiles.jsonl','groups.jsonl','bge-index.json','bge-documents.npy','bge-queries.npy'}
    verified=set()
    for line in (news/'ARTIFACTS.sha256').read_text().splitlines():
        expected,name=line.split(None,1)
        if name in wanted:
            if file_hash(news/name)!=expected:raise PipelineError('news_artifact_hash_mismatch')
            verified.add(name)
    if verified!=wanted:raise PipelineError('reference_artifact_hashes_missing')
    documents=rows(news/'documents.jsonl')
    matrix=np.load(news/'bge-documents.npy',allow_pickle=False)
    if [d['reference']['document_id'] for d in documents]!=index['document_ids'] or matrix.shape!=(len(documents),1024) or matrix.dtype!=np.float32:
        raise PipelineError('reference_mapping_or_matrix_mismatch')
    articles=[];items=[];vectors=[];revision_map={};raw_cache={}
    for d,v in zip(documents,matrix):
        r=d['reference'];legacy_rid=r['document_id']
        url=recover_url(d,raw_root,raw_cache)
        # URL enrichment creates an immutable metadata revision, never edits an old ID.
        rid='revision:'+digest([legacy_rid,'source-url.v1',url]) if url else legacy_rid
        revision_map[legacy_rid]=rid
        a=ArticleRevision(revision_id=rid,article_id=r['article_id'],url=url,text=d['text'],text_sha256=r['content_hash'],
            title=d['title'],title_origin=d['title_origin'],source=r['source'],published_at=r['published_at'],
            publication_precision=r['publication_precision'],observed_at=r.get('observed_at'),
            provenance=[dict(kind='explicit_bge_reference_conversion',reference=r,original=d['provenance'])])
        # Embedding text is unchanged; only source metadata and owner IDs are rebased.
        text=(a.title+'\n\n' if a.title_origin=='source' and a.title else '')+a.text
        item=SearchItem(item_id=rid,item_version=digest(a),title=a.title,text=a.text,embedding_text=text,source_revision_ids=[rid])
        vector=VectorRecord(vector_id=digest([rid,index['fingerprint']]),item_id=rid,item_version=item.item_version,
                            embedding_text_sha256=text_hash(text),values=v.tolist())
        articles.append(a);items.append(item);vectors.append(vector)
    known={a.revision_id for a in articles}
    groups=[]
    for row in rows(news/'groups.jsonl'):
        present=sorted(revision_map[x] for x in set(row['document_ids']) if x in revision_map);missing=sorted(set(row['document_ids'])-revision_map.keys())
        if not present:continue
        groups.append(ClusterSnapshot(cluster_id=row['group_id'],revision=digest([row,present]) if raw_root else digest(row),member_revision_ids=present,
            provenance=dict(interpretation=row['interpretation'],incomplete_membership=bool(missing),missing_revision_ids=missing)))
    corpus_id='bge-reference:'+file_hash(news/'documents.jsonl')
    (output/'articles.jsonl').write_text(''.join(canonical(a)+'\n' for a in articles))
    atomic_json(output/'clusters.json',[g.model_dump(mode='json') for g in groups])
    bundle=output/'native';bundle.mkdir(exist_ok=True)
    # NPY native format avoids expanding 3M coordinates into JSON.
    np.save(bundle/'vectors.npy',matrix,allow_pickle=False)
    mapping=[v.model_dump(mode='json',exclude={'values'}) for v in vectors]
    (bundle/'mapping.jsonl').write_text(''.join(canonical(v)+'\n' for v in mapping))
    atomic_json(bundle/'manifest.json',dict(schema_version='retriever-vector-bundle.v1',corpus_id=corpus_id,
        corpus_sha256=corpus_sha256(articles),article_sources={a.revision_id:a.text_sha256 for a in articles},
        encoder_fingerprint=index['fingerprint'],dimension=1024,items=[i.model_dump(mode='json') for i in items],
        matrix_file='vectors.npy',matrix_sha256=file_hash(bundle/'vectors.npy'),mapping_file='mapping.jsonl',mapping_sha256=file_hash(bundle/'mapping.jsonl')))
    read_vector_bundle(bundle,articles)
    topics=json.loads((topics_root/'interests.json').read_text())
    qmatrix=np.load(topics_root/'interest-vectors.npy',allow_pickle=False)
    profiles={p['company_id']:p for p in rows(news/'profiles.jsonl')}
    company_root=output/'companies';company_root.mkdir(exist_ok=True)
    entries=[];interest_counts={}
    for company in companies:
        p=profiles[company];code=p['okved']['code']
        selected=[t for t in topics if t['okved_code']==code and t['applicable_when'] is None]
        interests=CompanyInterestSet(company_id=company,profile_version=p['profile_version'],
            interest_set_version=digest([meta['identity']['catalogue_sha256'],[t['interest_id'] for t in selected]]),
            encoder_fingerprint=index['fingerprint'],dimension=1024,
            interests=[Interest(interest_id=t['interest_id'],text=t['description'],vector=qmatrix[t['row']].tolist(),
                provenance=dict(kind='activity_hypothesis',review_status=t['review_status'])) for t in selected])
        ip=interests.model_dump(mode='json')
        atomic_json(company_root/(company+'-profile.json'),p)
        atomic_json(company_root/(company+'-interests.json'),ip)
        display=dict(company_id=company,name=p['display_name'],industry=p['industry'] or 'Неизвестно',
            profile_version=p['profile_version'],is_synthetic=p['is_synthetic'],revenue_bn_rub=None,
            age_label='Неизвестно',region=p.get('region') or 'Неизвестно',products={},fts={},
            okved_code=p['okved']['code'] or '',okved_name=p['okved']['name'] or '')
        entries.append(dict(company_id=company,profile=company+'-profile.json',profile_sha256=digest(p),
            interests=company+'-interests.json',interests_sha256=digest(ip),display=display))
        interest_counts[company]=len(selected)
    atomic_json(company_root/'manifest.json',dict(schema_version='full-cycle-companies.v1',companies=entries))
    summary=dict(articles=len(articles),groups=len(groups),incomplete_groups=sum(bool(g.provenance['incomplete_membership']) for g in groups),
        fingerprint=index['fingerprint'],corpus_id=corpus_id,interests=interest_counts,
        source_urls_missing=sum(a.url is None for a in articles),source_hash=file_hash(news/'documents.jsonl'))
    summary['source_urls_recovered']=sum(a.url is not None for a in articles)
    atomic_json(output/'revision-map.json',revision_map)
    atomic_json(output/'preparation-report.json',summary)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--news',required=True);parser.add_argument('--topics',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--company',action='append',required=True)
    parser.add_argument('--raw-root',help='Original collector directory, validated by provenance hashes')
    args=parser.parse_args()
    print(json.dumps(prepare(args.news,args.topics,args.output,args.company,args.raw_root)))


if __name__=='__main__':main()
