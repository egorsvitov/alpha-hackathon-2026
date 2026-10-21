"""Conservative event identity and independent proposition comparison, no network."""
from decimal import Decimal
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from .catalog_models import ClaimContext, IdentityDescriptor, IdentityBasis, PairDecision, FieldComparison
from .storage import canonical_json

# Required identity slots. A missing slot is never equality evidence.
IDENTITY_MATRIX = {
    'policy_rate': ('actors', 'object', 'occurrence', 'stage'),
    'fx_rate': ('actors', 'object', 'metrics', 'rate_kind', 'effective'),
    'price_change': ('object', 'metrics', 'geography', 'observation'),
    'regulation': ('actors', 'object', 'geography', 'decision', 'stage'),
    'sanction_or_trade_restriction': ('actors', 'object', 'action', 'geography', 'decision', 'stage'),
    'market_indicator': ('population', 'object', 'metrics', 'geography', 'observation'),
    'company_results': ('actors', 'object', 'metrics', 'observation'),
    'investment': ('actors', 'object', 'geography', 'decision', 'stage'),
    'corporate_action': ('actors', 'object', 'action', 'decision', 'stage'),
    'operational_incident': ('actors', 'object', 'geography', 'observation'),
    'other': ('actors', 'object', 'action', 'occurrence'),
}
ACTION_ALIASES = {
    'сохранил': 'keep', 'сохранить': 'keep', 'оставил без изменений': 'keep', 'keep': 'keep',
    'повысил': 'raise', 'повысить': 'raise', 'raise': 'raise',
    'снизил': 'lower', 'снизить': 'lower', 'lower': 'lower',
}
RELEASE = re.compile(r'\b(?:выпуск|обзор|прогноз)\s*№\s*([\w./-]+)|\bвыпуск\s+от\s+(\d{4}-\d{2}-\d{2})', re.I)
DOCUMENT = re.compile(r'\b(закон|приказ|постановление|решение)\s*№\s*([\w./-]+)', re.I)


def rules_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def norm(value):
    return ' '.join(value.casefold().split()) if value else None


def encoded(value):
    return canonical_json(value)


def period_key(period):
    if period is None or period.start is None or period.end is None:
        return None
    return encoded([period.start, period.end])


def _refs(claim, path):
    return sorted({eid for p, ids in claim.field_evidence.items()
                   if p == path or path.startswith(p + '/') or p.startswith(path + '/') for eid in ids})


def describe_identity(context):
    ctx = ClaimContext.model_validate(context)
    c = ctx.claim
    features, bases = {}, []
    def add(name, value, path):
        if value is None or value == []:
            return
        features[name] = value if isinstance(value, str) else encoded(value)
        bases.append(IdentityBasis(feature=name, path=path, evidence_ids=_refs(c, path),
            transformation_paths=[str(i) for i,t in enumerate(ctx.transformations)
                                  if t.candidate_id == ctx.candidate_id and (t.path.startswith(path) or path.startswith(t.path))]))
    actors = [[p.role, p.entity_id] for p in c.participants if p.role not in ('forecaster', 'source', 'commentator')]
    if actors and all(eid for _,eid in actors):
        add('actors', sorted(actors), '/participants')
    add('object', norm(c.object), '/object')
    add('action', ACTION_ALIASES.get(norm(c.action), norm(c.action)), '/action')
    add('metrics', sorted({norm(m.metric) for m in c.measurements}), '/measurements')
    add('geography', sorted(c.geographies), '/geographies')
    if ctx.event_type=='fx_rate':
        metrics={m.metric for m in c.measurements}
        kind='official' if 'official_exchange_rate' in metrics else 'market' if 'market_exchange_rate' in metrics else None
        if kind:add('rate_kind',kind,'/measurements')
    if ctx.event_type=='market_indicator':
        if 'actors' in features:add('population',features['actors'],'/participants')
        elif c.object and c.geographies:
            add('population',[norm(c.object),sorted(c.geographies)],'/object')
            bases.append(IdentityBasis(feature='population',path='/geographies',evidence_ids=_refs(c,'/geographies')))
    add('stage', c.stage, '/stage')
    add('modality', c.modality if c.modality != 'unknown' else None, '/modality')
    # Forecast / assertion and plan / execution are distinct identities. Polarity
    # and numeric values are deliberately not universal identity keys.
    for name, field in [('occurrence','occurred_at'),('effective','effective_at'),
                        ('observation','observation_period'),('horizon','forecast_horizon')]:
        path = '/time/' + field
        assumed = any(t.candidate_id == ctx.candidate_id and
                      t.rule.startswith('nearest_period_year') and (path in t.path or t.path in path)
                      for t in ctx.transformations)
        if not assumed:
            add(name, period_key(getattr(c.time,field)), path)
    for name, pattern in [('document',DOCUMENT),('release',RELEASE)]:
        matches = []
        for evidence in ctx.evidence:
            if evidence.evidence_id not in c.evidence_ids:
                continue
            for match in pattern.finditer(evidence.quote):
                if name=='release' and match.group(2):
                    try:date.fromisoformat(match.group(2))
                    except ValueError:continue
                groups = [norm(x).rstrip('.') for x in match.groups() if x]
                matches.append((encoded(groups), evidence, match))
        if len({m[0] for m in matches}) == 1:
            value, e, match = matches[0]
            features[name] = value
            bases.append(IdentityBasis(feature=name,path=f'/evidence/{ctx.evidence.index(e)}/quote',evidence_ids=[e.evidence_id],
                source_id=e.source_id,field=e.field,start=e.start+match.start(),end=e.start+match.end()))
    if 'document' in features or 'occurrence' in features:
        features['decision'] = features.get('document',features.get('occurrence'))
        bases.extend([b.model_copy(update={'feature':'decision'}) for b in list(bases) if b.feature in ('document','occurrence')])
    required = list(IDENTITY_MATRIX[ctx.event_type]) + ['modality']
    if c.modality == 'forecast':
        required = [k for k in required if k not in ('stage','occurrence','decision','observation','effective')]
        required += ['author','horizon','release']
        add('author', c.attribution.entity_id if c.attribution else None, '/attribution')
    return IdentityDescriptor(claim_id=c.claim_id,event_type=ctx.event_type,features=features,
        missing=sorted(set(required)-features.keys()),bases=bases)


def _entity(e):
    return None if e is None else e.entity_id or ['unresolved', norm(e.name)]


def _period(p):
    return None if p is None else [p.start,p.end] if p.start and p.end else ['unresolved',p.text,p.start,p.end]


def _value(v):
    if isinstance(v,str):
        d=Decimal(v)
        if d==0:return '0'
        text=format(d,'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    return [_value(v.min),_value(v.max)]


def measurement_scope(m):
    return [norm(m.metric),m.kind,m.unit,m.value_role,_period(m.comparison_period)]


def semantic_claim(c):
    # B1 text is a paraphrase, and source wording is not the proposition's key.
    return encoded({
        'modality':c.modality,'polarity':c.polarity,'stage':c.stage,
        'author':_entity(c.attribution),
        'participants':sorted([encoded([p.role,_entity(p)]) for p in c.participants]),
        'action':ACTION_ALIASES.get(norm(c.action),norm(c.action)), 'object':norm(c.object),
        'geography':sorted(c.geographies),
        'time':{k:_period(v) for k,v in c.time.__dict__.items()},
        'scenario':None if c.scenario is None else [c.scenario.kind,norm(c.scenario.text)],
        'measurements':sorted(encoded([measurement_scope(m),_value(m.value),m.qualifier]) for m in c.measurements),
    })


def _interval(value):
    return (Decimal(value),Decimal(value)) if isinstance(value,str) else (Decimal(value.min),Decimal(value.max))


def compare_claims(left, right):
    left,right = ClaimContext.model_validate(left),ClaimContext.model_validate(right)
    a,b = left.claim,right.claim
    da,db = describe_identity(left),describe_identity(right)
    f,g = da.features,db.features
    # Additional known identity slots also veto a match; action is an outcome
    # for rate decisions and indicators, not the identity of the meeting.
    critical = set(IDENTITY_MATRIX[left.event_type]) | {'modality','author','horizon','release','document'}
    if 'decision' in critical:critical.add('occurrence')
    critical.discard('decision')  # compare its underlying document/date separately
    if a.modality == 'forecast' or b.modality == 'forecast':
        critical -= {'observation','effective'}
    differences = sorted(k for k in critical if k in f and k in g and f[k] != g[k])
    if left.event_type != right.event_type:
        differences.append('event_type')
    identity = 'different_event' if differences else 'uncertain' if da.missing or db.missing else 'same_event'
    reasons = ['different:'+k for k in differences] or ['missing:'+k for k in sorted(set(da.missing+db.missing))] or ['all_required_identity_features_match']
    decision = PairDecision(left_id=a.claim_id,right_id=b.claim_id,identity=identity,rules_hash=rules_hash(),
        reasons=reasons,bases=da.bases+db.bases,hard_differences=differences)
    if identity == 'same_event':
        return compare_content(left,right,decision)
    return decision


def comparable_periods(claim):
    periods=[p for p in claim.time.__dict__.values() if p is not None]
    periods += [m.comparison_period for m in claim.measurements if m.comparison_period is not None]
    return all(p.start is not None and p.end is not None for p in periods)


def compare_content(left,right,decision):
    """Compare propositions only after established event identity (including review)."""
    a,b = left.claim,right.claim
    fields = []
    same_scenario = (a.scenario is None and b.scenario is None) or (
        a.scenario is not None and b.scenario is not None and
        (a.scenario.kind,norm(a.scenario.text)) == (b.scenario.kind,norm(b.scenario.text)))
    if comparable_periods(a) and comparable_periods(b) and semantic_claim(a) == semantic_claim(b):
        return decision.model_copy(update={'content':'duplicate_claim'})
    # Do not compare numeric outcomes of different actions, unless they are
    # rate decisions whose level describes the same meeting outcome.
    comparable = same_scenario and a.modality == b.modality and a.polarity == b.polarity and comparable_periods(a) and comparable_periods(b)
    for i,x in enumerate(a.measurements):
        for j,y in enumerate(b.measurements):
            if not comparable or measurement_scope(x) != measurement_scope(y):
                continue
            # Repeated aspects inside a claim cannot be aligned safely.
            if sum(measurement_scope(m)==measurement_scope(x) for m in a.measurements)!=1 or sum(measurement_scope(m)==measurement_scope(y) for m in b.measurements)!=1:
                continue
            if x.qualifier == y.qualifier == 'exact':
                xmin,xmax = _interval(x.value); ymin,ymax = _interval(y.value)
                if xmax < ymin or ymax < xmin:
                    fields.append(FieldComparison(left_path=f'/measurements/{i}/value',right_path=f'/measurements/{j}/value'))
    if fields:
        decision = decision.model_copy(update={'content':'conflicts_with','fields':fields})
    else:
        decision = decision.model_copy(update={'content':'complements'})
    # Explicit correction of this article, never merely a later publication.
    if left.source.article_id == right.source.article_id and left.source.article_version != right.source.article_version:
        new,old = (left,right) if left.source.article_version > right.source.article_version else (right,left)
        if any(re.search(r'исправлен\w*|исправление|вместо ошибоч\w*',e.quote,re.I) for e in new.evidence):
            decision = decision.model_copy(update={'content':'updates','from_id':new.claim.claim_id,'to_id':old.claim.claim_id})
    return decision


def related_stages(left,right):
    """A shared explicit document, not a thematic association, links stages."""
    a,b = describe_identity(left),describe_identity(right)
    f,g = a.features,b.features
    required = ('document','actors','object','geography')
    order = {'proposed':0,'approved':2,'effective':3,'repealed':4}
    if left.event_type != right.event_type or left.claim.modality == 'forecast' or right.claim.modality == 'forecast':
        return None
    if any(k not in f or k not in g or f[k]!=g[k] for k in required):
        return None
    x,y=left.claim.stage,right.claim.stage
    if x not in order or y not in order or x==y:
        return None
    return (left.claim.claim_id,right.claim.claim_id) if order[x]>order[y] else (right.claim.claim_id,left.claim.claim_id)
