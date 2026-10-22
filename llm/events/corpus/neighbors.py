"""BM25 candidate neighborhoods for annotation, NOT canonical event clusters.

Groups overlap and may contain several events. Date filtering uses publication
calendar days, never an inferred event date. Original text stays in the corpus.
"""
from collections import Counter, defaultdict
from datetime import date, datetime
import hashlib
import json
from math import log
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from llm.events import StorageError
from llm.events.storage.store import canonical_json

STOP = set('и в во на с со к ко от до по за из у о об а но или это что как для при не ни он она они мы вы его ее их уже также еще чем который которая которые года году год этом этой этом был была были будет есть после перед между все всех'.split())
VERSION = 'article-neighborhoods.v1'


def terms(text):
    return [t for t in re.findall(r'[^\W_]+', text.casefold()) if len(t)>2 and t not in STOP and not t.isdecimal()]


def publication_day(doc):
    raw = doc['article']['published_at']
    if raw:
        return datetime.fromisoformat(raw).astimezone(ZoneInfo('Europe/Moscow')).date()
    dates = {p['publication']['date'] for p in doc['provenance'] if p['publication'].get('date')}
    return date.fromisoformat(next(iter(dates))) if len(dates)==1 else None


def load_corpus(directory):
    root = Path(directory)
    manifest = json.loads((root/'manifest.json').read_text())
    raw = (root/'documents.jsonl').read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['outputs']['documents.jsonl']:
        raise StorageError('corpus_output_corrupt', 'documents.jsonl')
    return manifest, [json.loads(line) for line in raw.splitlines() if line.strip()]


def candidate_neighborhoods(documents, *, top_k=3, per_source=20, max_groups=30, window_days=2,
                            start=None, end=None):
    if any(type(n) is not int or n<1 for n in (top_k, per_source, max_groups)) or type(window_days) is not int or window_days<0:
        raise StorageError('invalid_candidate_limits', 'Use positive finite limits and a nonnegative day window')
    lo = date.fromisoformat(start) if start else None
    hi = date.fromisoformat(end) if end else None
    if lo and hi and lo>hi: raise StorageError('invalid_date_range', 'start must not exceed end')
    docs = {d['document_id']:d for d in documents}
    if len(docs) != len(documents): raise StorageError('duplicate_document', 'Corpus document IDs must be unique')
    postings = defaultdict(dict); lengths = {}; counts = {}; days = {}
    repeated_lines = Counter(line for d in documents for line in {v.strip().casefold() for v in d['article']['text'].splitlines() if len(v.strip())>=20})
    removed = 0
    for did, doc in docs.items():
        article = doc['article']; days[did] = publication_day(doc)
        lines = article['text'].splitlines()
        # Retrieval view only: suppress very frequent complete lines, never edit source.
        kept = [line for line in lines if repeated_lines[line.strip().casefold()]<max(8,len(docs)*.03)]
        removed += len(lines)-len(kept)
        title = article['title'] if all(p['title_origin']=='source' for p in doc['provenance']) else ''
        tokens = terms(title+'\n'+title+'\n'+'\n'.join(kept))
        counts[did] = Counter(tokens); lengths[did] = len(tokens)
        for token,n in counts[did].items():postings[token][did] = n
    avg = sum(lengths.values())/max(1,len(docs)) or 1
    by_source = defaultdict(list)
    for did in sorted(docs):
        day = days[did]
        if day and (not lo or lo<=day) and (not hi or day<=hi):
            by_source[docs[did]['article']['source'] or 'unknown'].append(did)
    anchors = [values[i] for i in range(per_source) for source,values in sorted(by_source.items()) if i<len(values)]
    groups=[]; no_match=[]; seen=set()
    for anchor in anchors:
        scores=defaultdict(float); shared=Counter()
        for token in counts[anchor]:
            positions=postings[token]; df=len(positions)
            if df>max(3,len(docs)*.15):continue
            idf=log(1+(len(docs)-df+.5)/(df+.5))
            for other,tf in positions.items():
                if (other==anchor or not days[other] or abs((days[anchor]-days[other]).days)>window_days
                    or docs[anchor]['article']['source']==docs[other]['article']['source']):continue
                scores[other]+=idf*tf*2.2/(tf+1.2*(.25+.75*lengths[other]/avg));shared[other]+=1
        ranked=[(other,score) for other,score in scores.items() if shared[other]>=3]
        ranked.sort(key=lambda pair:(-pair[1],pair[0]))
        if not ranked:no_match.append(anchor);continue
        selected=ranked[:top_k];members=tuple(sorted([anchor]+[other for other,_ in selected]))
        if members in seen:continue
        seen.add(members)
        groups.append({'group_id':hashlib.sha256(canonical_json(list(members)).encode()).hexdigest()[:24],
            'anchor_id':anchor,'document_ids':list(members),'candidates':[
                {'document_id':did,'bm25_score':score,'shared_index_terms':shared[did]} for did,score in selected],
            'identity_status':'unassessed','split':'unassigned','annotation_status':'unannotated'})
        if len(groups)>=max_groups:break
    return {'schema_version':VERSION,'groups':groups,'unmatched_anchors':no_match,
        'configuration':{'top_k':top_k,'per_source':per_source,'max_groups':max_groups,'window_days':window_days,
                         'start':start,'end':end,'cross_source_only':True,'k1':1.2,'b':.75},
        'retrieval_view':{'suppressed_common_line_occurrences':removed,'source_text_modified':False},
        'limitations':['Candidate neighborhoods may overlap and mix distinct events.',
                       'BM25 scores are retrieval ranks, not identity probabilities.',
                       'Unmatched documents are not negative labels.',
                       'No development/test split until event families are manually checked.']}


def write_preview(corpus_dir, output, **kwargs):
    manifest, docs = load_corpus(corpus_dir)
    report = candidate_neighborhoods(docs, **kwargs)
    report['corpus_id']=manifest['corpus_id']
    root=Path(output)
    if root.exists():raise StorageError('preview_exists', 'Choose a new preview output directory')
    root.mkdir(parents=True)
    (root/'neighbors.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lookup={d['document_id']:d for d in docs}
    selected={did for g in report['groups'] for did in g['document_ids']}
    (root/'documents.jsonl').write_text(''.join(canonical_json(lookup[did])+'\n' for did in sorted(selected)))
    lines=['# Предварительные группы для ручного просмотра','',
           'Это кандидаты для совместного чтения, не установленные события. Полные тексты — documents.jsonl.',
           'Текст ниже сокращён только для предпросмотра. Разметка и split ещё не назначены.','']
    for i,g in enumerate(report['groups'],1):
        lines.extend([f'## Группа {i}: {g["group_id"]}',''])
        for did in [g['anchor_id']]+[c['document_id'] for c in g['candidates']]:
            article=lookup[did]['article']
            lines.extend([f'### {article["source"]}: {article["title"]}',
                          f'`{did}` · {article["published_at"] or "точное время неизвестно"}','',
                          article['text'][:900]+'\n',''])
    (root/'REVIEW.md').write_text('\n'.join(lines))
    return {'status':'prepared','output':str(root),'groups':len(report['groups']),
            'documents':len(selected),'identity_status':'unassessed'}
