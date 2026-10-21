"""B5 explicit input selection, reproducible snapshots and optimistic publication."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
from pathlib import Path
from uuid import uuid4, uuid5, NAMESPACE_URL
from .catalog_models import (CatalogConfig, ClaimContext, CanonicalEventRecord, PairDecision,
                             ReviewRequest)
from .candidate_search import BM25CandidateSearch, pair_key
from .grouping import cluster_claims
from .matching import compare_claims, compare_content, related_stages, semantic_claim
from .models import Conflict, ConflictVariant, Relation
from .normalization import validate_normalized_result
from .normalization_store import NormalizationService
from .normalization_models import NormalizedArticleResult
from .storage import StorageError, canonical_json
from .validation import article_sha256, _parse
from .catalog_validation import validate_catalog_record


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def implementation_hash():
    paths = ['matching.py','candidate_search.py','catalog_store.py','catalog_models.py','grouping.py','catalog_validation.py']
    return digest({name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in paths})


def decision_id(d):
    return str(uuid5(NAMESPACE_URL,'alpha-b5-decision:'+digest(d.model_dump(mode='json',exclude={'retrieval_methods','retrieval_score'}))))


def _stamp(epoch):
    return datetime.fromtimestamp(epoch,timezone.utc).isoformat()


class CatalogService:
    def __init__(self,store,catalog_id='default',*,search=None):
        if not isinstance(catalog_id,str) or not catalog_id.strip():
            raise StorageError('invalid_argument','catalog_id must be non-empty')
        self.store,self.catalog_id=store,catalog_id
        self.search=search or BM25CandidateSearch()

    def create_catalog(self,config=None):
        config=_parse(CatalogConfig,config or CatalogConfig())
        packed={'config':config.model_dump(mode='json'),'implementation_hash':implementation_hash(),'retriever':self.search.configuration()}
        with self.store._transaction(write=True) as conn:
            row=conn.execute('SELECT * FROM catalogs WHERE catalog_id=?',(self.catalog_id,)).fetchone()
            if row:
                if json.loads(row['config_json'])!=packed:
                    raise StorageError('catalog_configuration_mismatch','Use explicit rebuild to change rules')
            else:
                conn.execute('INSERT INTO catalogs(catalog_id,config_json,created_at) VALUES (?,?,?)',
                             (self.catalog_id,canonical_json(packed),self.store.clock()))
        return {'catalog_id':self.catalog_id,'status':'ready'}

    def _capture(self):
        with self.store._transaction() as conn:
            row=self.store._require(conn.execute('SELECT * FROM catalogs WHERE catalog_id=?',(self.catalog_id,)).fetchone(),'catalog',self.catalog_id)
            data=dict(row)
            data['inputs']={r['article_id']:dict(r) for r in conn.execute('SELECT * FROM catalog_inputs WHERE catalog_id=?',(self.catalog_id,))}
            data['heads']={r['article_id']:r['current_source_id'] for r in conn.execute('SELECT * FROM article_heads')}
            data['reviews']=[dict(r) for r in conn.execute('SELECT * FROM catalog_reviews WHERE catalog_id=? ORDER BY created_order',(self.catalog_id,))]
            run=conn.execute('SELECT * FROM catalog_runs WHERE catalog_id=? ORDER BY revision DESC LIMIT 1',(self.catalog_id,)).fetchone()
            data['run']=dict(run) if run else None
            data['events']=[]
            for event in conn.execute('SELECT * FROM catalog_events WHERE catalog_id=? ORDER BY created_order,event_id',(self.catalog_id,)):
                latest=conn.execute('SELECT payload_json FROM catalog_event_versions WHERE event_id=? ORDER BY event_version DESC LIMIT 1',(event['event_id'],)).fetchone()
                data['events'].append({'meta':dict(event),'record':CanonicalEventRecord.model_validate(json.loads(latest['payload_json']))})
        return data

    def _load_artifact(self,artifact_id):
        artifact=self.store.get_artifact(artifact_id)
        service=NormalizationService(self.store)
        owner=service._owner(artifact.attempt_id)
        if artifact.kind!='normalized_claims' or artifact.schema_version!='b4.1' or owner.spec.processor_name!='events.normalize_claims' or owner.state!='succeeded' or owner.result_artifact_id!=artifact_id:
            raise StorageError('invalid_catalog_input','Expected a successful final normalized_claims B4 artifact')
        result=_parse(NormalizedArticleResult,artifact.payload)
        record=self.store.get_article(owner.source_id)
        if result.source.source_id!=owner.source_id or result.article_sha256!=article_sha256(record.article) or result.configuration_sha256!=owner.spec.configuration.get('configuration_sha256') or result.input_artifact_id!=owner.spec.configuration.get('input_artifact_id'):
            raise StorageError('input_provenance_mismatch','B4 payload does not match its processing job')
        upstream,upowner,_,uprecord=service.input_artifact(result.input_artifact_id)
        if upowner.spec_hash!=owner.spec.configuration.get('input_processing_spec_hash') or uprecord!=record:
            raise StorageError('input_provenance_mismatch','B4 upstream does not match its processing job')
        validate_normalized_result(result,record)
        if result.quality=='insufficient':
            raise StorageError('invalid_catalog_input','Insufficient B4 results cannot be selected')
        return result

    def _archived_context(self,claim_id):
        with self.store._transaction() as conn:
            row=conn.execute('SELECT r.manifest_json FROM catalog_memberships m JOIN catalog_runs r USING(run_id) WHERE r.catalog_id=? AND m.claim_id=? ORDER BY r.revision DESC LIMIT 1',(self.catalog_id,claim_id)).fetchone()
        if row is None:raise StorageError('claim_not_found',claim_id)
        return next(ClaimContext.model_validate(c) for c in json.loads(row['manifest_json'])['contexts'] if c['claim']['claim_id']==claim_id)

    def _contexts(self,result,artifact_id):
        return [ClaimContext(artifact_id=artifact_id,candidate_id=a.candidate_id,event_type=a.event_type,
            claim=a.claim,source=result.source,
            evidence=[e for e in result.evidence if e.evidence_id in a.claim.evidence_ids],
            transformations=result.transformations,
            diagnostics=[d for d in result.diagnostics if d.candidate_id in (None,a.candidate_id)],
            limitations=sorted(set(result.limitations+([f'B4 quality={result.quality}'] if result.quality!='complete' else [])+
                [d.issue.code+': '+d.issue.message for d in result.diagnostics]))) for a in result.accepted]

    @staticmethod
    def _effective_reviews(rows):
        revoked={r['revoked_review_id'] for r in rows if r['revoked_review_id']}
        effective={}
        for row in rows:
            if row['review_id'] not in revoked and not row['revoked_review_id']:
                effective[pair_key(row['left_id'],row['right_id'])]=json.loads(row['payload_json'])
        return effective

    def _prepare(self,artifact_ids=(),*,replace=False,config=None,rebuild=False,new_review=None,revoke=None):
        if not isinstance(artifact_ids,(list,tuple)):
            raise StorageError('invalid_argument','artifact_ids must be a finite list, not a string')
        if type(replace) is not bool:raise StorageError('invalid_argument','replace must be a boolean')
        before=self._capture();packed=json.loads(before['config_json'])
        config=_parse(CatalogConfig,config or packed['config'])
        configuration={'config':config.model_dump(mode='json'),'implementation_hash':implementation_hash(),'retriever':self.search.configuration()}
        if not rebuild and configuration!=packed:
            raise StorageError('catalog_configuration_mismatch','Rules changed; explicit rebuild is required')
        selected={k:dict(v) for k,v in before['inputs'].items()}
        incoming={}
        for aid in sorted(set(artifact_ids)):
            r=self._load_artifact(aid);article_id=r.source.article_id
            if before['heads'].get(article_id)!=r.source.source_id:
                raise StorageError('article_not_current','Only current B4 artifacts can be selected')
            if article_id in incoming and incoming[article_id]!=aid:
                raise StorageError('ambiguous_catalog_input','Two results for one article in a batch')
            incoming[article_id]=aid
            prior=selected.get(article_id)
            if prior and prior['artifact_id']!=aid and prior['source_id']==r.source.source_id and not replace:
                raise StorageError('catalog_replace_required','Use replace to select a different B4 result of the same revision')
            selected[article_id]={'article_id':article_id,'source_id':r.source.source_id,'artifact_id':aid}
        contexts={};missing=[];empty=[]
        for article_id,selection in sorted(selected.items()):
            if before['heads'].get(article_id)!=selection['source_id']:
                missing.append(article_id);continue
            result=self._load_artifact(selection['artifact_id'])
            if not result.accepted:empty.append(article_id)
            for ctx in self._contexts(result,selection['artifact_id']):
                if ctx.claim.claim_id in contexts:
                    raise StorageError('duplicate_id','Different inputs contain the same claim ID')
                contexts[ctx.claim.claim_id]=ctx
        if len(contexts)>config.max_claims:
            raise StorageError('catalog_size_limit','Active claim count exceeds max_claims')
        rows=before['reviews'][:]
        pending_review=None
        historical={}
        if revoke:
            target=next((r for r in rows if r['review_id']==revoke and not r['revoked_review_id']),None)
            if target is None or any(r['revoked_review_id']==revoke for r in rows):
                raise StorageError('review_not_active','Review does not exist or was already revoked')
            pending_review={'review_id':str(uuid4()),'left_id':target['left_id'],'right_id':target['right_id'],
                            'payload_json':canonical_json({'reason':new_review}),'revoked_review_id':revoke}
        elif new_review is not None:
            request=_parse(ReviewRequest,new_review)
            if not request.reason.strip():raise StorageError('invalid_argument','A non-empty reason is required')
            key=pair_key(request.left_id,request.right_id)
            if request.content=='updates':
                for cid in key:
                    if cid not in contexts:historical[cid]=self._archived_context(cid)
                if request.from_id not in contexts:
                    raise StorageError('invalid_update_direction','The replacing claim must be current')
            if key[0]==key[1] or any(cid not in contexts and cid not in historical for cid in key):
                raise StorageError('invalid_review_pair','Choose two distinct current claims (an updates target may be archived)')
            if request.left_id!=key[0]:
                raise StorageError('invalid_pair_order','left_id must be lexicographically less than right_id')
            a,b=((contexts.get(cid) or historical[cid]) for cid in key)
            raw=compare_claims(a,b)
            if request.identity=='same_event' and raw.hard_differences:
                raise StorageError('incompatible_identity','Cannot override explicit differences: '+','.join(raw.hard_differences))
            decided=raw.model_copy(update={'identity':request.identity,'origin':'manual','reasons':[request.reason],
                'content':'none','fields':[],'from_id':None,'to_id':None})
            if decided.identity=='same_event':decided=compare_content(a,b,decided)
            if request.content=='duplicate_claim' and semantic_claim(a.claim)!=semantic_claim(b.claim):
                raise StorageError('incompatible_duplicate','Distinct propositions cannot be declared duplicates')
            if request.content=='conflicts_with':
                automatic=compare_content(a,b,decided)
                if request.identity!='same_event' or automatic.content!='conflicts_with':
                    raise StorageError('conflict_not_proven','Need the same aspect and incompatible exact values')
                if request.fields and request.fields!=automatic.fields:
                    raise StorageError('invalid_conflict_reference','Use the matching numeric field paths')
                decided=automatic
            elif request.content=='updates':
                if {request.from_id,request.to_id}!=set(key):
                    raise StorageError('invalid_update_direction','Specify new and replaced claim IDs')
                decided=decided.model_copy(update={'content':'updates','from_id':request.from_id,'to_id':request.to_id,'fields':[]})
            elif request.content!='none':
                if request.identity!='same_event':raise StorageError('invalid_content_relation','Content relation requires same_event')
                decided=decided.model_copy(update={'content':request.content})
            pending_review={'review_id':str(uuid4()),'left_id':key[0],'right_id':key[1],
                'payload_json':canonical_json(decided.model_dump(mode='json')),'revoked_review_id':None}
        if pending_review:rows.append(pending_review)
        effective=self._effective_reviews(rows)
        reviews={k:v for k,v in effective.items() if all(cid in contexts for cid in k)}
        groups,decisions,candidates=cluster_claims(contexts,config,reviews,self.search)
        archival_reviews={k:v for k,v in effective.items() if v.get('content')=='updates' and v.get('from_id') in contexts and not all(cid in contexts for cid in k)}
        for key,value in archival_reviews.items():
            historical[value['to_id']]=self._archived_context(value['to_id'])
            decisions[key]=PairDecision.model_validate(value)
        # Source corrections may explicitly replace any formerly active claim,
        # not just the founding anchor of its event.
        old_contexts=json.loads(before['run']['manifest_json'])['contexts'] if before['run'] else []
        history_checks=0
        for payload in old_contexts:
            previous=ClaimContext.model_validate(payload)
            if previous.claim.claim_id in contexts:continue
            for current in contexts.values():
                if current.source.article_id!=previous.source.article_id:continue
                history_checks+=1
                if len(decisions)+history_checks>config.max_comparisons:
                    raise StorageError('catalog_comparison_limit','Historical checks exceed max_comparisons')
                d=compare_claims(current,previous)
                if d.content=='updates':decisions[pair_key(d.left_id,d.right_id)]=d

        manifest={'inputs':[{k:v for k,v in selection.items() if k!='catalog_id'} for _,selection in sorted(selected.items())],
            'heads':{aid:before['heads'].get(aid) for aid in sorted(selected)},
            'configuration':configuration,'reviews':reviews_to_json(reviews|archival_reviews),
            'missing_articles':missing,'empty_articles':empty,
            'contexts':[contexts[cid].model_dump(mode='json') for cid in sorted(contexts)]}
        fingerprint=digest(manifest)
        records,new_meta=self._records(before,contexts,groups,decisions,missing,config.max_comparisons-history_checks)
        return {'before':before,'configuration':configuration,'selected':selected,'manifest':manifest,
                'fingerprint':fingerprint,'groups':groups,'decisions':decisions,'records':records,
                'new_meta':new_meta,'pending_review':pending_review}

    def _records(self,before,contexts,groups,decisions,missing,max_comparisons):
        checks=len(decisions)
        def bounded_compare(a,b):
            nonlocal checks
            checks+=1
            if checks>max_comparisons:raise StorageError('catalog_comparison_limit','Identity history checks exceed max_comparisons')
            return compare_claims(a,b)
        # A former event proposes one successor group, prioritising its anchor.
        proposals=defaultdict(list)
        confirmed_history=defaultdict(set)
        for d in decisions.values():
            if d.origin!='manual' or d.identity!='same_event':continue
            for current,archived in ((d.left_id,d.right_id),(d.right_id,d.left_id)):
                if current not in contexts or archived in contexts:continue
                with self.store._transaction() as conn:
                    row=conn.execute('SELECT m.event_id FROM catalog_memberships m JOIN catalog_runs r USING(run_id) WHERE r.catalog_id=? AND m.claim_id=? ORDER BY r.revision DESC LIMIT 1',(self.catalog_id,archived)).fetchone()
                if row:confirmed_history[row['event_id']].add(current)
        for old in before['events']:
            record=old['record'];anchor=old['meta']['anchor_claim_id']
            prior={c.claim_id for c in record.claims}
            overlaps=[i for i,g in enumerate(groups) if prior.intersection(g)]
            if overlaps:
                chosen=next((i for i in overlaps if anchor in groups[i]),None)
                if chosen is None:chosen=min(overlaps,key=lambda i:min(prior.intersection(groups[i])))
                proposals[chosen].append(old)
            else:
                identity=ClaimContext.model_validate(json.loads(old['meta']['identity_json']))
                compatible=[i for i,g in enumerate(groups) if confirmed_history[record.event_id].intersection(g) or all(bounded_compare(contexts[cid],identity).identity=='same_event' for cid in g)]
                if compatible:proposals[min(compatible,key=lambda i:groups[i][0])].append(old)
        assigned={};new_meta=[];owner={};now=_stamp(self.store.clock())
        order=max([e['meta']['created_order'] for e in before['events']],default=0)
        for i,g in enumerate(groups):
            choices=sorted(proposals[i],key=lambda e:(e['meta']['created_order'],e['meta']['event_id']))
            if choices:event_id=choices[0]['meta']['event_id'];anchor=choices[0]['meta']['anchor_claim_id']
            else:
                event_id=str(uuid5(NAMESPACE_URL,canonical_json(['alpha-b5-event',self.catalog_id,before['revision']+1,g[0]])))
                anchor=g[0];order+=1
                new_meta.append({'event_id':event_id,'created_order':order,'anchor_claim_id':anchor,
                                 'identity_json':canonical_json(contexts[anchor].model_dump(mode='json'))})
            assigned[i]=(event_id,anchor)
            for cid in g:owner[cid]=event_id
        old_by_id={e['meta']['event_id']:e['record'] for e in before['events']}
        records={}
        for i,g in enumerate(groups):
            eid,anchor=assigned[i];entries=[contexts[cid] for cid in g]
            claims=[e.claim for e in entries]
            sources={e.source.source_id:e.source for e in entries}
            evidence={v.evidence_id:v for e in entries for v in e.evidence}
            conflicts=[];relations=[];dids=[]
            for key,d in sorted(decisions.items()):
                if set(key)<=set(g):
                    dids.append(decision_id(d))
                    if d.content=='duplicate_claim':relations.append(Relation(type='duplicate_claim',from_id=d.left_id,to_id=d.right_id,target_kind='claim'))
                    if d.content=='conflicts_with':
                        for field in d.fields:
                            conflicts.append(Conflict(conflict_id=str(uuid5(NAMESPACE_URL,decision_id(d)+field.left_path+field.right_path)),
                                description='Incompatible exact values for the same event and measurement aspect',
                                variants=[ConflictVariant(claim_id=d.left_id,field_path=field.left_path),ConflictVariant(claim_id=d.right_id,field_path=field.right_path)]))
                if d.content=='updates' and d.from_id in g:
                    dids.append(decision_id(d))
                    relations.append(Relation(type='updates',from_id=d.from_id,to_id=d.to_id,target_kind='claim'))
                if (d.left_id in g or d.right_id in g) and d.left_id in contexts and d.right_id in contexts:
                    later=related_stages(contexts[d.left_id],contexts[d.right_id])
                    if later and owner[later[0]]==eid and owner[later[1]]!=eid:
                        relations.append(Relation(type='related_stage',from_id=eid,to_id=owner[later[1]],target_kind='event'))
            limits={v for e in entries for v in e.limitations}
            if missing:limits.add('Missing current B4 results for: '+', '.join(missing))
            relevant=[d for key,d in decisions.items() if set(key).intersection(g) and all(cid in contexts for cid in key)]
            if any(d.identity=='uncertain' for d in relevant):limits.add('Some candidate event identities remain uncertain')
            if any(d.identity=='same_event' and owner.get(d.left_id)!=owner.get(d.right_id) for d in relevant):limits.add('Some same-event pairs could not join a fully compatible group')
            relations=sorted({canonical_json(r.model_dump()):r for r in relations}.values(),key=lambda r:canonical_json(r.model_dump()))
            record=CanonicalEventRecord(catalog_id=self.catalog_id,event_id=eid,event_version=1,event_type=entries[0].event_type,
                availability='active',updated_at=now,anchor_claim_id=anchor,claims=claims,
                sources=[sources[sid] for sid in sorted(sources)],evidence=[evidence[x] for x in sorted(evidence)],
                conflicts=conflicts,relations=relations,limitations=sorted(limits),decision_ids=sorted(set(dids)))
            records[eid]=self._version(record,old_by_id.get(eid))
        for eid,old in old_by_id.items():
            if eid in records:continue
            replacements=sorted({owner[c.claim_id] for c in old.claims if c.claim_id in owner})
            if not replacements:
                replacements=sorted({assigned[i][0] for i,choices in proposals.items() if any(e['meta']['event_id']==eid for e in choices)})
            withdrawn=old.model_copy(update={'availability':'withdrawn','claims':[],'sources':[],'evidence':[],
                'conflicts':[],'relations':[],'decision_ids':[],'replaced_by':replacements,
                'limitations':['No active claims remain; this is not a refutation'],'updated_at':now})
            records[eid]=self._version(withdrawn,old)
        archive={source.source_id:self.store.get_article(source.source_id) for record in records.values() for source in record.sources}
        for record in records.values():validate_catalog_record(record,list(archive.values()))
        return records,new_meta

    @staticmethod
    def _version(record,old):
        if old is None:return record
        ignored={'event_version','updated_at'}
        if record.model_dump(exclude=ignored)==old.model_dump(exclude=ignored):return old
        return record.model_copy(update={'event_version':old.event_version+1})

    def _publish(self,plan):
        before=plan['before'];last=before['run']
        with self.store._transaction(write=True) as conn:
            row=self.store._require(conn.execute('SELECT * FROM catalogs WHERE catalog_id=?',(self.catalog_id,)).fetchone(),'catalog',self.catalog_id)
            heads={r['article_id']:r['current_source_id'] for r in conn.execute('SELECT * FROM article_heads')}
            if (row['revision'],row['generation'])!=(before['revision'],before['generation']) or any(heads.get(aid)!=before['heads'].get(aid) for aid in plan['selected']):
                raise StorageError('catalog_changed','Catalog inputs or source heads changed during calculation')
            # Review cancellation itself is audited even when it leaves content unchanged.
            if last and last['fingerprint']==plan['fingerprint'] and not plan['pending_review'] and not row['stale']:
                return {'status':'cached','catalog_id':self.catalog_id,'revision':before['revision'],'run_id':last['run_id'],'changed_event_ids':[]}
            revision=before['revision']+1;run_id=str(uuid4());now=self.store.clock()
            review=plan['pending_review']
            if review:
                order=conn.execute('SELECT COALESCE(MAX(created_order),0)+1 FROM catalog_reviews WHERE catalog_id=?',(self.catalog_id,)).fetchone()[0]
                conn.execute('INSERT INTO catalog_reviews VALUES (?,?,?,?,?,?,?,?)',(review['review_id'],self.catalog_id,order,now,review['left_id'],review['right_id'],review['payload_json'],review['revoked_review_id']))
            conn.execute('DELETE FROM catalog_inputs WHERE catalog_id=?',(self.catalog_id,))
            for aid,v in sorted(plan['selected'].items()):
                conn.execute('INSERT INTO catalog_inputs VALUES (?,?,?,?)',(self.catalog_id,aid,v['source_id'],v['artifact_id']))
            conn.execute('INSERT INTO catalog_runs VALUES (?,?,?,?,?,?,?)',(run_id,self.catalog_id,revision,plan['fingerprint'],now,
                canonical_json(plan['manifest']),canonical_json([d.model_dump(mode='json') for _,d in sorted(plan['decisions'].items())])))
            for m in plan['new_meta']:
                conn.execute('INSERT INTO catalog_events VALUES (?,?,?,?,?)',(m['event_id'],self.catalog_id,m['created_order'],m['anchor_claim_id'],m['identity_json']))
            changed=[]
            old={e['record'].event_id:e['record'] for e in before['events']}
            for eid,record in sorted(plan['records'].items()):
                if eid not in old or old[eid].event_version!=record.event_version:
                    conn.execute('INSERT INTO catalog_event_versions VALUES (?,?,?)',(eid,record.event_version,canonical_json(record.model_dump(mode='json'))));changed.append(eid)
                conn.execute('INSERT INTO catalog_run_events VALUES (?,?,?)',(run_id,eid,record.event_version))
                for claim in record.claims:
                    artifact=next(c['artifact_id'] for c in plan['manifest']['contexts'] if c['claim']['claim_id']==claim.claim_id)
                    conn.execute('INSERT INTO catalog_memberships VALUES (?,?,?,?)',(run_id,claim.claim_id,eid,artifact))
            conn.execute('UPDATE catalogs SET revision=?,generation=generation+1,stale=0,config_json=? WHERE catalog_id=?',
                (revision,canonical_json(plan['configuration']),self.catalog_id))
        result={'status':'succeeded','catalog_id':self.catalog_id,'revision':revision,'run_id':run_id,
                'changed_event_ids':changed,'missing_articles':plan['manifest']['missing_articles']}
        if review:result['review_id']=review['review_id']
        return result

    def plan_catalog_update(self,artifact_ids=(),*,replace=False):
        plan=self._prepare(artifact_ids,replace=replace)
        return {'status':'planned','catalog_id':self.catalog_id,'base_revision':plan['before']['revision'],
            'groups':plan['groups'],'missing_articles':plan['manifest']['missing_articles'],
            'decisions':[d.model_dump(mode='json') for _,d in sorted(plan['decisions'].items())]}

    def apply_artifacts(self,artifact_ids=(),*,replace=False):
        return self._publish(self._prepare(artifact_ids,replace=replace))

    def rebuild_catalog(self,config=None):
        return self._publish(self._prepare(config=config,rebuild=True))

    def review_pair(self,request):
        return self._publish(self._prepare(new_review=request))

    def revoke_review(self,review_id,reason):
        if not isinstance(reason,str) or not reason.strip():raise StorageError('invalid_argument','A reason is required')
        return self._publish(self._prepare(new_review=reason,revoke=review_id))

    def _read_run(self,conn,revision):
        row=self.store._require(conn.execute('SELECT * FROM catalogs WHERE catalog_id=?',(self.catalog_id,)).fetchone(),'catalog',self.catalog_id)
        if revision is None and row['stale']:raise StorageError('catalog_stale','Rebuild before reading the current catalog; history requires explicit revision')
        revision=row['revision'] if revision is None else revision
        return self.store._require(conn.execute('SELECT * FROM catalog_runs WHERE catalog_id=? AND revision=?',(self.catalog_id,revision)).fetchone(),'catalog_revision',str(revision))

    def list_events(self,*,revision=None,include_withdrawn=False):
        with self.store._transaction() as conn:
            run=self._read_run(conn,revision)
            records=[CanonicalEventRecord.model_validate(json.loads(r['payload_json'])) for r in conn.execute('SELECT v.payload_json FROM catalog_run_events r JOIN catalog_event_versions v USING(event_id,event_version) WHERE r.run_id=? ORDER BY r.event_id',(run['run_id'],))]
        return [r for r in records if include_withdrawn or r.availability=='active']

    def get_event(self,event_id,*,event_version=None,revision=None):
        if event_version is not None:
            with self.store._transaction() as conn:
                row=conn.execute('SELECT v.payload_json FROM catalog_event_versions v JOIN catalog_events e USING(event_id) WHERE v.event_id=? AND v.event_version=? AND e.catalog_id=?',(event_id,event_version,self.catalog_id)).fetchone()
                return CanonicalEventRecord.model_validate(json.loads(self.store._require(row,'event_version',event_id)['payload_json']))
        record=next((r for r in self.list_events(revision=revision,include_withdrawn=True) if r.event_id==event_id),None)
        if record is None:raise StorageError('event_not_found',event_id)
        return record

    def changes_since(self,revision):
        if type(revision) is not int or revision<0:raise StorageError('invalid_argument','Revision must be a nonnegative integer')
        with self.store._transaction() as conn:
            current=self._read_run(conn,None)
            if revision>current['revision']:raise StorageError('invalid_argument','Revision is in the future')
            rows=conn.execute('SELECT r.revision,e.event_id,e.event_version FROM catalog_runs r JOIN catalog_run_events e USING(run_id) WHERE r.catalog_id=? AND r.revision>? ORDER BY r.revision,e.event_id',(self.catalog_id,revision)).fetchall()
            old={r['event_id']:r['event_version'] for r in conn.execute('SELECT e.* FROM catalog_runs r JOIN catalog_run_events e USING(run_id) WHERE r.catalog_id=? AND r.revision=?',(self.catalog_id,revision))}
            changes=[]
            for row in rows:
                if old.get(row['event_id'])!=row['event_version']:
                    changes.append(dict(row));old[row['event_id']]=row['event_version']
        return {'catalog_id':self.catalog_id,'revision':current['revision'],'changes':changes}

    def list_decisions(self,*,revision=None,uncertain_only=False):
        with self.store._transaction() as conn:
            run=self._read_run(conn,revision)
            decisions=[PairDecision.model_validate(d) for d in json.loads(run['decisions_json'])]
        return [d for d in decisions if not uncertain_only or d.identity=='uncertain']

    def get_recommendation(self,artifact_id):
        from .pair_check import get_recommendation
        return get_recommendation(self,artifact_id)

    def check_pair(self,*args,**kwargs):
        from .pair_check import check_pair
        return check_pair(self,*args,**kwargs)

    def list_reviews(self):
        return self._capture()['reviews']


def reviews_to_json(reviews):
    return [{'pair':list(k),'decision':v} for k,v in sorted(reviews.items())]
