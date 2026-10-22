"""Deterministic B5 -> B6 projection, with explicit version/time and no storage writes."""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from llm.events.catalog.models import CanonicalEventRecord
from llm.events.catalog.validation import validate_catalog_record
from llm.events.contracts.models import Event, EventBundle, RankingFeatures, Issue
from llm.events.normalization.core import validate_normalized_result
from llm.events.publication.models import PublicationConfig, PublicationContext, FeatureDictionary, FeatureTrace, FeatureBasis, BundleProjection
from llm.events.storage.store import canonical_json, StorageError
from llm.events.contracts.validation import _parse, _covers, pointer, validate_bundle, validate_grounding

ROOT=Path(__file__).parent
VERIFICATION_LIMIT='Проверены структура и точные цитаты; смысловая поддержка и истинность независимо не оценены.'


def digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def resolved_rules(config):
    return config.dictionary or _parse(FeatureDictionary,json.loads((ROOT/'publication_vocabulary.json').read_text()))


def publication_configuration_hash(config):
    return digest({'config':config.model_dump(mode='json'),'dictionary':resolved_rules(config).model_dump(mode='json'),
        'implementation':{p:hashlib.sha256((ROOT.parent/p).read_bytes()).hexdigest() for p in
            ('publication/features.py','publication/models.py','publication/builder.py','publication/service.py','publication/export.py','contracts/validation.py','catalog/validation.py')},
        'bundle_schema':EventBundle.model_json_schema()})


def _normalized(value):
    return ' '.join(value.casefold().split())


def _basis(claim,path):
    refs=sorted({eid for p,ids in claim.field_evidence.items() if _covers(p,path) for eid in ids})
    return FeatureBasis(claim_id=claim.claim_id,path=path,evidence_ids=refs)


def derive_features(record,rules):
    """Return labels plus every rule/basis assignment, never a company effect."""
    labels={k:set() for k in ('topics','industries','geographies','objects','metrics','factors')}
    support=defaultdict(dict)
    def add(group,label,rule,bases):
        labels[group].add(label)
        entry=support[(group,label,rule)]
        for basis in bases:entry[canonical_json(basis.model_dump(mode='json'))]=basis
    for claim in record.claims:
        for i,value in enumerate(claim.geographies):add('geographies',value,'copy.geography',[_basis(claim,f'/geographies/{i}')])
        if claim.object:add('objects',claim.object,'copy.object',[_basis(claim,'/object')])
        for i,m in enumerate(claim.measurements):add('metrics',m.metric,'copy.metric',[_basis(claim,f'/measurements/{i}/metric')])
    # Each rule is a conjunction across its non-empty criteria. Values within
    # one criterion are alternatives. All field criteria must match one claim.
    for rule in rules.rules:
        condition=rule.when
        if condition.event_types and record.event_type not in condition.event_types:continue
        for claim in record.claims:
            bases=[];matched=True
            if condition.event_types:
                bases.append(FeatureBasis(claim_id=claim.claim_id,path='/event_type',evidence_ids=[],classification=True))
            for name,values in (('objects',condition.objects),('metrics',condition.metrics),('actions',condition.actions)):
                if not values:continue
                accepted={_normalized(v) for v in values}
                candidates=([(claim.object,'/object')] if name=='objects' else [(claim.action,'/action')] if name=='actions' else
                            [(m.metric,f'/measurements/{i}/metric') for i,m in enumerate(claim.measurements)])
                found=[_basis(claim,path) for value,path in candidates if value and _normalized(value) in accepted]
                if not found:matched=False;break
                bases.extend(found)
            if not matched:continue
            for group in ('topics','industries','factors'):
                for label in getattr(rule.labels,group):add(group,label,rule.rule_id,bases)
    if not labels['topics']:
        add('topics','other','fallback.topic',[FeatureBasis(claim_id=None,path='/event_type',evidence_ids=[],classification=True)])
    sorted_labels={k:sorted(v) for k,v in labels.items()}
    traces=[]
    for (group,label,rule),bases in sorted(support.items()):
        traces.append(FeatureTrace(path=f'/ranking_features/{group}/{sorted_labels[group].index(label)}',label=label,rule_id=rule,
            bases=[bases[k] for k in sorted(bases)]))
    return RankingFeatures(**sorted_labels),traces


def assess_quality(record,context):
    reasons=[]
    def reason(code,path,message):reasons.append(Issue(code=code,path=path,message=message))
    if record.availability=='withdrawn':
        reason('event_withdrawn','/event/availability','Событие отозвано; текущих оснований для анализа нет.')
        return 'insufficient',reasons
    anchored=[bool(c.action or c.object or c.measurements) for c in record.claims]
    if not any(anchored):
        reason('no_structured_subject','/claims','Нет структурированного действия, объекта или показателя для анализа.')
        return 'insufficient',reasons
    source_ids={s.source_id for s in record.sources}
    for item in context.normalized_inputs:
        result=item.result
        if result.source.source_id not in source_ids:continue
        if result.quality!='complete':reason('upstream_partial','/claims',f'Неполная нормализация источника {result.source.source_id}: {result.quality}.')
        for d in result.diagnostics:
            reason('b4_'+d.issue.code,d.issue.path,d.issue.message)
        if result.rejected and result.quality=='complete':
            raise StorageError('input_provenance_mismatch','B4 complete result contains rejected candidates')
    if context.missing_articles:reason('missing_current_inputs','/sources','Нет текущего B4 для: '+', '.join(context.missing_articles))
    ids={c.claim_id for c in record.claims};current=set(context.catalog_claim_ids)|ids
    for d in context.decisions:
        if (not ids.intersection((d.left_id,d.right_id)) or
                not {d.left_id,d.right_id}<=current or d.content=='updates'):continue
        if d.identity=='uncertain':reason('identity_uncertain','/claims','Принадлежность части кандидатных утверждений событию не установлена.')
        if d.identity=='same_event' and {d.left_id,d.right_id}<=current and not {d.left_id,d.right_id}<=ids:
            reason('incomplete_cluster','/claims','Некоторые совпадающие пары не входят в полностью совместимую группу.')
    for i,c in enumerate(record.claims):
        path=f'/claims/{i}'
        if not anchored[i]:reason('claim_subject_missing',path,'У утверждения отсутствует структурированный предмет.')
        if c.modality=='unknown':reason('modality_unknown',path+'/modality','Модальность не установлена.')
        if c.polarity=='unknown':reason('polarity_unknown',path+'/polarity','Отрицание не установлено.')
        def usable(p):return p is not None and (p.start is not None or p.end is not None)
        if c.modality=='forecast':
            valid=usable(c.time.forecast_horizon)
        elif record.event_type in ('company_results','market_indicator','price_change') and c.modality=='asserted' and c.measurements:
            valid=usable(c.time.observation_period)
        elif record.event_type=='fx_rate':
            valid=usable(c.time.effective_at) or usable(c.time.observation_period)
        elif record.event_type in ('policy_rate','regulation','sanction_or_trade_restriction','corporate_action','investment','operational_incident') or c.action:
            valid=usable(c.time.occurred_at) or usable(c.time.effective_at)
        else:valid=True
        if not valid:reason('essential_time_unknown',path+'/time','Существенная временная привязка не установлена.')
        if record.event_type in ('policy_rate','regulation','sanction_or_trade_restriction') and c.modality=='asserted' and c.stage is None:
            reason('decision_stage_unknown',path+'/stage','Стадия утверждения о решении не установлена.')
    reasons=list({canonical_json(r.model_dump()):r for r in reasons}.values())
    return ('partial' if reasons else 'complete'),reasons


def _title(record,rules):
    title=rules.category_titles[record.event_type]
    claims=record.claims
    common=None
    for c in claims:
        names={v for v in [c.object,*[m.metric for m in c.measurements]] if v in rules.subject_titles}
        common=names if common is None else common & names
    if common:
        subject=rules.subject_titles[sorted(common)[0]]
        if subject!=title:title+=': '+subject
    modalities={c.modality for c in claims}
    prefixes={'forecast':'Прогноз','plan':'План','proposal':'Предложение','opinion':'Оценка'}
    if len(modalities)==1 and next(iter(modalities)) in prefixes:return prefixes[next(iter(modalities))]+': '+title
    if len(modalities)!=1 or 'unknown' in modalities:return 'Сведения: '+title
    return title


def build_bundle(record,context,config=None,*,public_version,published_at):
    record=_parse(CanonicalEventRecord,record);context=_parse(PublicationContext,context)
    config=_parse(PublicationConfig,config or PublicationConfig());rules=resolved_rules(config)
    validate_catalog_record(record,context.articles)
    archives={a.source_id:a for a in context.articles}
    relevant={s.source_id for s in record.sources}
    upstream={}
    for item in context.normalized_inputs:
        result=item.result
        if result.source.source_id not in relevant:continue
        if result.source.source_id not in archives:raise StorageError('missing_article','Archived source is missing')
        validate_normalized_result(result,archives[result.source.source_id])
        if result.source.source_id in upstream:raise StorageError('input_provenance_mismatch','Multiple B4 results for one source')
        upstream[result.source.source_id]=result
    if context.normalized_inputs:
        for claim in record.claims:
            sid=next(e.source_id for e in record.evidence if e.evidence_id in claim.evidence_ids)
            result=upstream.get(sid)
            if result is None or not any(a.claim==claim and a.event_type==record.event_type for a in result.accepted):
                raise StorageError('input_provenance_mismatch','B5 claim differs from selected B4 input')
    record=record.model_copy(update={'claims': sorted(record.claims,key=lambda c:c.claim_id)})
    quality,reasons=assess_quality(record,context)
    features,traces=derive_features(record,rules)
    limits=set(record.limitations)|{VERIFICATION_LIMIT}|{r.message for r in reasons}
    for s in record.sources:
        if s.content_kind=='source_based_paraphrase':limits.add(f'Источник {s.source_id} — пересказ, а не дословный полный оригинал.')
        elif not s.is_full_original_text:limits.add(f'Для источника {s.source_id} полный оригинал не заявлен.')
    if record.availability=='withdrawn' and record.replaced_by:limits.add('Событие заменено: '+', '.join(sorted(record.replaced_by)))
    dates=[s.published_at for s in record.sources if s.published_at]
    bundle=EventBundle(schema_version='0.2',event=Event(event_id=record.event_id,event_version=public_version,
        title=_title(record,rules),event_type=record.event_type,
        first_published_at=min(dates,key=datetime.fromisoformat) if dates else None,
        updated_at=published_at,availability=record.availability,quality=quality),ranking_features=features,
        claims=sorted(record.claims,key=lambda c:c.claim_id),sources=sorted(record.sources,key=lambda s:s.source_id),
        evidence=sorted(record.evidence,key=lambda e:e.evidence_id),conflicts=sorted(record.conflicts,key=lambda c:c.conflict_id),
        relations=sorted(record.relations,key=lambda r:canonical_json(r.model_dump())),limitations=sorted(limits))
    validate_bundle(bundle);validate_grounding(bundle,context.articles)
    result=BundleProjection(bundle=bundle,feature_trace=traces,quality_reasons=reasons)
    validate_feature_trace(result)
    return result


def validate_feature_trace(projection):
    projection=_parse(BundleProjection,projection);raw=projection.bundle.model_dump(mode='json')
    claims={c.claim_id:c for c in projection.bundle.claims};evidence={e.evidence_id for e in projection.bundle.evidence}
    covered=set()
    for trace in projection.feature_trace:
        try:value=pointer(raw,trace.path)
        except (ValueError,KeyError,IndexError,TypeError):raise StorageError('invalid_feature_trace','Feature path is absent') from None
        if not trace.path.startswith('/ranking_features/') or value!=trace.label or not trace.bases:
            raise StorageError('invalid_feature_trace','Feature trace does not identify its output label')
        covered.add(trace.path)
        for base in trace.bases:
            if base.classification:
                if base.claim_id is not None and base.claim_id not in claims:
                    raise StorageError('invalid_feature_trace','Unknown classification claim')
                if base.path!='/event_type' or base.evidence_ids:raise StorageError('invalid_feature_trace','Classification cannot invent evidence')
            else:
                if base.claim_id not in claims:raise StorageError('invalid_feature_trace','Unknown supporting claim')
                c=claims[base.claim_id]
                try:pointer(c.model_dump(mode='json'),base.path)
                except (ValueError,KeyError,IndexError,TypeError):raise StorageError('invalid_feature_trace','Supporting field is absent') from None
                if not base.evidence_ids or not set(base.evidence_ids)<=evidence or not set(base.evidence_ids)<=set(_basis(c,base.path).evidence_ids):
                    raise StorageError('invalid_feature_trace','Supporting field lacks its actual evidence')
    expected={f'/ranking_features/{k}/{i}' for k,values in raw['ranking_features'].items() for i in range(len(values))}
    if covered!=expected:raise StorageError('invalid_feature_trace','Every emitted label needs a trace')
    return projection
