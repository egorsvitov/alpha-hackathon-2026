"""Finite numeric/unit grammar backed by linked literal evidence."""
from decimal import Decimal, localcontext
import re
from llm.events.storage.store import StorageError

NUM=re.compile(r'[+−-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?')
SCALES={'тыс':Decimal(1000),'млн':Decimal(10)**6,'млрд':Decimal(10)**9,'трлн':Decimal(10)**12}
SCALE=re.compile(r'\b(тыс|млн|млрд|трлн)\.?',re.I)
UNITS=[
 ('RUB/USD',r'rub/usd|руб(?:лей|ля)?\s+(?:за|/)\s*доллар\w*',Decimal(1)),
 ('RUB/EUR',r'rub/eur|руб(?:лей|ля)?\s+(?:за|/)\s*евро',Decimal(1)),
 ('RUB/CNY',r'rub/cny|руб(?:лей|ля)?\s+(?:за|/)\s*юан\w*',Decimal(1)),
 ('percentage_point',r'percentage_point|процентн\w*\s+пункт\w*|п\.?\s*п\.',Decimal(1)),
 ('basis_point',r'basis_point|базисн\w*\s+пункт\w*|б\.?\s*п\.',Decimal(1)),
 ('percent',r'percent\b|%|процент(?:ов|а|ы)?\b',Decimal(1)),
 ('barrel_per_day',r'barrel_per_day|баррел\w*\s+(?:в\s+сутки|/\s*сут\w*)',Decimal(1)),
 ('bit_per_second',r'тбит\s*/\s*с|tbps',Decimal(10)**12),
 ('bit_per_second',r'гбит\s*/\s*с|gbps',Decimal(10)**9),
 ('bit_per_second',r'мбит\s*/\s*с|mbps',Decimal(10)**6),
 ('bit_per_second',r'bit_per_second|(?<![а-яa-z])бит\s*/\s*с|(?<![а-яa-z])бит\w*\s+в\s+секунду',Decimal(1)),
 ('ip_address_count',r'ip_address_count|ip[- ]адрес\w*',Decimal(1)),
 ('RUB',r'\brub\b|₽|\bруб(?:\.|ль|ля|лей|лях|лями)?\b',Decimal(1)),
 ('RUB',r'\bкоп(?:\.|ейк\w*)?\b',Decimal('0.01')),
 ('USD',r'\busd\b|\$|доллар\w*',Decimal(1)),
 ('EUR',r'\beur\b|€|евро',Decimal(1)),
 ('CNY',r'\bcny\b|юан\w*',Decimal(1)),
 ('tonne',r'\btonne\b|тонн\w*|\bт\b',Decimal(1)),
 ('hour',r'\bhour\b|час(?:а|ов)?\b|\bч\b',Decimal(1)),
 ('rank',r'\brank\b|мест(?:о|а|е)\b',Decimal(1)),
 ('count',r'\bcount\b|штук\w*|\bшт\b|единиц\w*|организаци\w*|компани\w*|магазин\w*|атак\w*|эксперт\w*',Decimal(1)),
]
DIRECTION=re.compile(r'(?P<positive>увелич\w*|вырос\w*|возрос\w*|рост\w*|прирост\w*|прибав\w*|выше|больше|повыс\w*)|(?P<negative>сниз\w*|сниж\w*|сократ\w*|сокращ\w*|упал\w*|паден\w*|меньше|ниже|уменьш\w*|ослаб\w*)',re.I)
QUALIFIERS=[('approximate',r'примерно|приблизительно|около|порядка|~|≈'),('gte',r'не\s+менее|>=|≥'),('lte',r'не\s+более|<=|≤'),('gt',r'более|больше\s+чем|>'),('lt',r'менее|ниже\s+чем|<')]


def decimal_text(value):
    if value==0:return '0'
    return format(value,'f').rstrip('0').rstrip('.') if '.' in format(value,'f') else format(value,'f')


def _number(raw):
    return Decimal(re.sub(r'[ \u00a0\u202f]','',raw).replace(',','.').replace('−','-'))


def _scale(text):
    scales={SCALES[m[1].casefold()] for m in SCALE.finditer(text)}
    if len(scales)>1:raise StorageError('conflicting_scale','Different magnitude multipliers')
    return next(iter(scales)) if scales else None


def _unit(text):
    matches={(unit,factor) for unit,pattern,factor in UNITS if re.search(pattern,text,re.I)}
    pairs={x for x in matches if '/' in x[0]}
    if pairs:matches=pairs
    if len(matches)!=1:
        raise StorageError('unsupported_unit' if not matches else 'conflicting_unit','Expected one recognized unit')
    return next(iter(matches))


def _occurrences(text,number):
    return [m for m in NUM.finditer(text) if abs(_number(m[0]))==abs(number)]


def _local(text,match):
    # Decimal separators do not terminate a clause. Coordinating conjunctions do.
    boundaries=list(re.finditer(r'(?<!\d)[;.!?]|[;.!?](?!\d)|\s+(?:но|и|однако)\s+',text,re.I))
    left=max([b.end() for b in boundaries if b.end()<=match.start()]+[0])
    right=min([b.start() for b in boundaries if b.start()>=match.end()]+[len(text)])
    return text[left:match.start()],text[match.end():right]


def normalize_measurement(measurement,value_quotes,unit_quotes):
    raw=measurement.value_text
    tokens=list(NUM.finditer(raw))
    ordinal={'первое':1,'второе':2,'третье':3,'первый':1}
    if not tokens and raw.casefold() in ordinal:
        numbers=[Decimal(ordinal[raw.casefold()])]
        if not any(raw in q for q in value_quotes):raise StorageError('number_not_quoted','Ordinal is absent from linked evidence')
    else:
        if len(tokens) not in (1,2):raise StorageError('unsupported_number','Expected one number or one range')
        if len(tokens)==2 and not (tokens[1][0].startswith('-') and tokens[0].end()==tokens[1].start()) and not re.fullmatch(r'\s*(?:[-–—]|до|\.\.)\s*',raw[tokens[0].end():tokens[1].start()]):
            raise StorageError('unsupported_number','Unsupported numeric range')
        if any(len(re.sub(r'[^0-9]','',m[0]))>80 for m in tokens):raise StorageError('unsupported_number','Numeric token exceeds the exact arithmetic limit')
        numbers=[_number(m[0]) for m in tokens]
        # A leading hyphen in the second endpoint can be the range delimiter.
        if len(tokens)==2 and tokens[1][0].startswith('-') and tokens[0].end()==tokens[1].start():
            numbers[1]=abs(numbers[1])
        if any(not any(_occurrences(q,n) for q in value_quotes) for n in numbers):
            raise StorageError('number_not_quoted','Value is absent from its linked evidence')
    if len(numbers)==2 and numbers[0]>numbers[1]:raise StorageError('reversed_range','Numeric range is reversed')
    a,b=_scale(raw),_scale(measurement.unit_text)
    if a and b and a!=b:raise StorageError('conflicting_scale','Value and unit specify different magnitudes')
    scale=a or b or Decimal(1)
    unit,factor=_unit(measurement.unit_text)
    if not any(any(u==unit and f==factor and re.search(pattern,q,re.I) for u,pattern,f in UNITS) for q in unit_quotes):
        raise StorageError('unit_not_quoted','Unit is absent from its linked evidence')
    # Verify a magnitude actually used beside this numeric occurrence (including
    # a shared `from X to Y <unit>` suffix), not elsewhere in a long quotation.
    if scale!=1:
        supported=set()
        for q in value_quotes+unit_quotes:
            for index,n in enumerate(numbers):
                for match in _occurrences(q,n):
                    tail=q[match.end():]
                    immediate=re.match(r'\s*(тыс|млн|млрд|трлн)\.?',tail,re.I)
                    shared=re.match(r'\s*(?:до|[-–—])\s*'+NUM.pattern+r'\s*(тыс|млн|млрд|трлн)\.?',tail,re.I)
                    if any(m and SCALES[m[1].casefold()]==scale for m in (immediate,shared)):supported.add(index)
        if len(supported)!=len(numbers):raise StorageError('scale_not_quoted','Magnitude is not attached to this value/range')
    for quote in value_quotes:
        for number in numbers:
            for match in _occurrences(quote,number):
                tail=quote[match.end():]
                next_number=NUM.search(tail)
                if next_number:tail=tail[:next_number.start()]
                tail=re.split(r'[;!?]|\s+(?:но|и|а)\s+',tail,maxsplit=1,flags=re.I)[0]
                try:local_unit,local_factor=_unit(tail)
                except StorageError:continue
                if local_unit!=unit or local_factor!=factor:
                    raise StorageError('unit_mismatch','Local value unit conflicts with the candidate unit')
    if measurement.kind=='level' and tokens:
        for number in numbers:
            supported=False
            for quote in value_quotes:
                for match in _occurrences(quote,number):
                    literal=_number(match[0])
                    if match[0].startswith('-') and match.start()>0 and quote[match.start()-1].isdigit():
                        literal=abs(literal)  # ASCII range separator, not a minus sign.
                    if literal==number:supported=True
            if not supported:raise StorageError('number_sign_mismatch','Level sign differs from the literal reported value')
    if measurement.kind=='relative_change' and unit!='percent':raise StorageError('kind_unit_mismatch','Relative change must use percent')
    if unit in {'percentage_point','basis_point'} and measurement.kind!='absolute_change':raise StorageError('kind_unit_mismatch','Points must express absolute change')
    if unit=='percent' and measurement.kind=='absolute_change':raise StorageError('kind_unit_mismatch','Absolute percentage change requires explicit points')
    signs=set();qualifiers=set()
    for q in value_quotes:
        for n in numbers:
            for match in _occurrences(q,n):
                prefix,suffix=_local(q,match)
                directions=list(DIRECTION.finditer(prefix))
                # Comparative `на X% меньше` is local to X and takes precedence.
                post=re.match(r'\s*(?:%|процент\w*|копе\w*|рубл\w*|тонн\w*|пункт\w*)?\s*(больше|меньше|выше|ниже)\b',suffix,re.I)
                if post:signs.add(-1 if post[1].casefold() in {'меньше','ниже'} else 1)
                elif directions:signs.add(-1 if directions[-1].lastgroup=='negative' else 1)
                previous=list(NUM.finditer(prefix))
                local_prefix=prefix[previous[-1].end():] if previous else prefix
                for qualifier,pattern in QUALIFIERS:
                    if re.search(r'(?:'+pattern+r')(?:\s+(?:на|до|с))?\s*$',local_prefix,re.I):
                        qualifiers.add(qualifier);break
    if qualifiers and qualifiers!={measurement.qualifier}:raise StorageError('qualifier_mismatch','Qualifier conflicts with linked text')
    if not qualifiers and measurement.qualifier!='exact':
        raise StorageError('qualifier_not_quoted','Non-exact qualifier has no local textual basis')
    if measurement.kind!='level':
        if len(signs)!=1:raise StorageError('ambiguous_direction','No unique direction attached to the measurement')
        sign=next(iter(signs))
        for i,n in enumerate(numbers):
            explicit=tokens[i][0].startswith(('-','−','+')) if tokens else False
            if explicit and n!=0 and (1 if n>0 else -1)!=sign:
                raise StorageError('direction_mismatch','Explicit sign conflicts with linked direction')
        numbers=[abs(n)*sign for n in numbers]
    with localcontext() as ctx:
        ctx.prec=100
        values=[n*scale*factor for n in numbers]
    if len(values)==2:
        if measurement.qualifier not in {'exact','approximate'}:raise StorageError('invalid_range_qualifier','Range requires exact/approximate qualifier')
        # A negative change range changes endpoint order during sign conversion.
        value={'min':decimal_text(min(values)),'max':decimal_text(max(values))}
    else:value=decimal_text(values[0])
    return value,unit,{'rule':'decimal_unit_and_direction','multiplier':str(scale*factor)}
