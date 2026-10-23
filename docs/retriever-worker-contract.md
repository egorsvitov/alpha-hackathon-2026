# Worker coordination contract (2026-10-24)

Worktree /home/pavel/projects/hackathons/alpha-retriever, branch retriever.
Root owns models.py and ports.py (already written), selection.py, CLI, docs and integration.
Do not edit other worker files. Read docs/new-retriever-implementation-plan.md.

Models live retriever/models.py. Do not invent divergent field names. Imports are standalone.

Search/import worker:
- exact_search(snapshot: IndexSnapshot, interests: CompanyInterestSet, *, include_graph=False, block_size=512) -> list[SearchHit], all candidates sorted (-score,item_id), max fragments then interests.
- import functions: read_articles(path) -> (list[ArticleRevision], diagnostics), read_clusters(path,articles) -> (list[ClusterSnapshot],diagnostics), read_vector_bundle(path,articles) -> dict with keys items,vectors,encoder_fingerprint,dimension,corpus_id, read_interests(path)->CompanyInterestSet. Choose/document manifest schema; source hashes mandatory, corpus hash/IDs validated.
- Need standard JSON bundle support for CLI plus old artifacts as optional explicit adapters, no import old NLP. Do not silently cutoff long text. New revision must reflect metadata+body. Parser rows7fields/prepared wrapper supported.

Store worker:
- SQLiteSearchStore(path), contextmanager/close optional; snapshot()->IndexSnapshot
- import_bundle(*,articles,items,vectors,clusters,encoder_fingerprint,dimension,corpus_id)->dict. Transactional/idempotent; whole initial snapshot, same corpus/profile enforced; inserting conflicting immutable IDs fails.
- get_sources(revision_ids)->list[ArticleRevision] sorted; get_item(item_id, include_inactive=False)->SearchItem|None
- clusters_for_item(item_id)->list[ClusterSnapshot], get_cluster_snapshot(cluster_id)->ClusterSnapshot
- set_clusters(clusters)->dict update memberships/revision, restore originals and invalidate merged representations as needed; reject changed same revision, detect all overlap and mark ambiguous (do not choosefirst).
- apply_merge(expected:ClusterSnapshot,result:llm.cluster_merge.models.MergeResult,encoded_items:list[EncodedItem],*,request:llm.cluster_merge.models.MergeRequest)->ApplyResult. Validate request/result original fragment references using llm.cluster_merge.core.validate_merge_result and extra full output/cardinality validation as needed. Never trust caller result. Verify revision/hash/full expected live originals under transaction. Idempotent result application checks BEFORE pending check; duplicate event IDs sharing source valid. unchanged marks resolved and keeps raws. Atomic vectors+items+state. Return stale on CAS conflict; no mutation.
- root supplies fully encoded event SearchItems with derived_from:{operation_id,result_id,cluster_id,cluster_revision,configuration_fingerprint,event:<event.model_dump()>}. vector embeddinghash must match item embedding text unlessfragment explicit issuefirstMVPvectorrecordhash identifiesfragment not wholeitem; for article-fragment allow trusted importmanifest text hashes; event full vector exact.

Merge adapter worker:
- ClusterMergeAdapter(service,profile=None), configuration_fingerprint property from core.configuration(MergeRequest(...)) (configuration independent of article set).
- resolve(cluster,articles,trigger_id,*,max_calls,deadline_seconds=None)->ports.MergeResolution(status,operation_id,result,request,new_calls,reason). Build article exactrefs/titleonlysourceorigins. Use existing service, no duplicated transport/retry. new_calls accurate from shared limiter reservation or robust service bookkeeping (result.calls cumulative, NOT per-request!). Budget enforce across operations root sumsnewcalls. Need accurate count on thrownerrors, concurrency. Prefer peradapter scoped countedlimiter wrapper via separate MergeService instance sameprovider/path/clock; avoid mutating caller shared service limiter. No newquota. Standard adapter is sync; timeout communicate external provider profile maxdeadline and enforce bestpossible/documentbounded behavior, don't spawn abandoned backgroundpaidcalls.
- run existing merge tests, audit correctness/security semantic checks/unknownfailed outcomes. Fix only actual critical service defect with regression tests; communicate before modifying colleaguefiles. No live API on this stage.

Root builds RetrieverService.retrieve(request,*,interests,store,merge=None,encoder=None)->RetrievalRun. Offline lookupstillallowed maxcalls0. encoder onlyneeded on completed unapplied outputs. Scripts fake providers and encoders suffice.

Environment .venv is being installed locally. Fallback for tests /home/pavel/projects/hackathons/alpha/.venv_events/bin/python (pydantic2.14.1,np2.5.4 installed).
No git commit/push by workers. Never read .env keys or publish rawdatasets.
