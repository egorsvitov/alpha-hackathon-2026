"""Small deterministic BM25 channel; no external search service required."""
from collections import Counter
import math
import re
import unicodedata

VERSION = 'bm25-unicode.v1'
STOP = set('и в во на с со по для от до о об а но или из к у это компания компании запись регионе регион отрасль'.split())

def tokens(text):
    return [w for w in re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', text).casefold().replace('ё','е')) if w not in STOP and len(w)>1]

def search_lexical(index, texts, *, top_k=50, id_field='event_id'):
    if id_field not in {'event_id', 'document_id'}:
        raise ValueError('unsupported identity field')
    query = set(tokens(' '.join(texts)))
    if not hasattr(index,'_bm25_documents'):
        index._bm25_documents={uid: Counter(tokens(text)) for uid,text in index.documents.items()}
        index._bm25_df=Counter(w for d in index._bm25_documents.values() for w in d)
    docs=index._bm25_documents
    n=len(docs)
    if not n or not query: return []
    average=sum(sum(d.values()) for d in docs.values())/n or 1
    df=index._bm25_df
    result=[]
    for uid,doc in docs.items():
        score=0.; matched=[]; length=sum(doc.values())
        for word in sorted(query & doc.keys()):
            tf=doc[word];idf=math.log(1+(n-df[word]+.5)/(df[word]+.5))
            score+=idf*tf*2.2/(tf+1.2*(.25+.75*length/average));matched.append(word)
        if score>0: result.append({id_field:uid,'channel':'lexical','raw_score':score,'matched_terms':matched})
    return [dict(hit,rank=i) for i,hit in enumerate(sorted(result,key=lambda h:(-h['raw_score'],h[id_field]))[:top_k],1)]
