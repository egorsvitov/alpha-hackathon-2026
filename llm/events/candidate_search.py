"""Replaceable B5 candidate search: structural blocks plus BM25, no models."""
from collections import Counter, defaultdict
from itertools import combinations
from math import log
import re
from .matching import describe_identity
from .storage import StorageError


def pair_key(a,b):
    return tuple(sorted((a,b)))


def tokens(text):
    return re.findall(r'[^\W_]+',text.casefold(),re.UNICODE)


class BM25CandidateSearch:
    def configuration(self):
        return {'name':'bm25_structural','version':'b5.1','k1':1.2,'b':0.75}

    def search(self,contexts,config,manual_pairs=()):
        if len(contexts)>config.max_claims:
            raise StorageError('catalog_size_limit','Active claim count exceeds max_claims')
        result={}
        def add(a,b,method,score=0.0):
            if a==b:return
            key=pair_key(a,b)
            if key not in result:
                if len(result)>=config.max_comparisons:
                    raise StorageError('catalog_comparison_limit','Candidate count exceeds max_comparisons')
                result[key]={'methods':set(),'score':0.0}
            result[key]['methods'].add(method)
            result[key]['score']=max(result[key]['score'],float(score))
        blocks=defaultdict(list)
        by_type=defaultdict(list)
        for ctx in sorted(contexts,key=lambda c:c.claim.claim_id):
            cid=ctx.claim.claim_id;f=describe_identity(ctx).features
            by_type[ctx.event_type].append(ctx)
            if 'actors' in f and 'object' in f:
                blocks[(ctx.event_type,'actors_object',f['actors'],f['object'])].append(cid)
            for time in ('occurrence','effective','observation','horizon','document'):
                if 'metrics' in f and time in f:
                    blocks[(ctx.event_type,'metric_time',f['metrics'],time,f[time])].append(cid)
        for ids in blocks.values():
            for a,b in combinations(ids,2):add(a,b,'structure')
        for group in by_type.values():
            inverted=defaultdict(dict);lengths={};counts={}
            for ctx in group:
                c=ctx.claim;cid=c.claim_id
                terms=tokens(' '.join([c.text,c.object or '',c.action or '',*[p.name for p in c.participants],*[m.metric for m in c.measurements]]))
                counts[cid]=Counter(terms);lengths[cid]=len(terms)
                for term,n in counts[cid].items():inverted[term][cid]=n
            avg=sum(lengths.values())/len(group) or 1
            for cid,query in counts.items():
                scores=defaultdict(float)
                for term in query:
                    postings=inverted[term];df=len(postings);idf=log(1+(len(group)-df+0.5)/(df+0.5))
                    for other,tf in postings.items():
                        if other!=cid:
                            scores[other]+=idf*tf*2.2/(tf+1.2*(0.25+0.75*lengths[other]/avg))
                for other,score in sorted(scores.items(),key=lambda item:(-item[1],item[0]))[:config.top_k]:
                    add(cid,other,'bm25',score)
        for a,b in manual_pairs:add(a,b,'manual')
        return result
