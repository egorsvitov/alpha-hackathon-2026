"""Small auditable calendar grammar. No system clock or fuzzy date parser."""
import calendar
from dataclasses import dataclass
from datetime import date, datetime
import re
from llm.events.storage.store import StorageError

MONTHS={'янв':1,'фев':2,'мар':3,'апр':4,'май':5,'мая':5,'мае':5,'июн':6,'июл':7,'авг':8,'сен':9,'окт':10,'ноя':11,'дек':12}
MONTH_RE=re.compile(r'\b(?:январ\w*|феврал\w*|март\w*|апрел\w*|май|мая|мае|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\b',re.I)
YEAR_RE=re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')


@dataclass
class PeriodResult:
    period: dict
    rule: str
    warnings: list[str]
    metadata_refs: list[str]


def _end(year,month):return date(year,month,calendar.monthrange(year,month)[1])

def _shift_year(day,offset):
    return day.replace(year=day.year+offset,day=min(day.day,calendar.monthrange(day.year+offset,day.month)[1]))


def normalize_period(text,published_at,*,context='',observation=None):
    raw=text;t=' '.join(text.casefold().split());refs=[]
    published=datetime.fromisoformat(published_at).date() if published_at else None
    def result(start=None,end=None,rule='unresolved_period',warnings=None):
        return PeriodResult({'start':start.isoformat() if start else None,'end':end.isoformat() if end else None,'text':raw},rule,warnings or [],refs.copy())
    def unknown(code='unresolved_period'):return result(warnings=[code])
    # Explicit comparisons inherit only an established observation interval.
    if re.search(r'год\s+к\s+году|годом\s+ранее|к\s+тому\s+же\s+периоду\s+прошлого\s+года|в\s+годовом\s+(?:сравнении|сопоставлении)',t):
        if observation and observation.get('start') and observation.get('end'):
            return result(_shift_year(date.fromisoformat(observation['start']),-1),_shift_year(date.fromisoformat(observation['end']),-1),'comparison_previous_year')
        return unknown('comparison_anchor_unknown')
    if re.search(r'к\s+предыдущему\s+месяцу|месяц\s+к\s+месяцу',t):
        if observation and observation.get('start') and observation.get('end'):
            start=date.fromisoformat(observation['start']);end=date.fromisoformat(observation['end'])
            if start.day==1 and start.year==end.year and start.month==end.month and end==_end(end.year,end.month):
                year,month=(start.year,start.month-1) if start.month>1 else (start.year-1,12)
                return result(date(year,month,1),_end(year,month),'comparison_previous_month')
        return unknown('comparison_anchor_unknown')
    iso_matches=list(re.finditer(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)',t))
    iso=iso_matches[0] if iso_matches else None
    if len(iso_matches)>1:
        if len(iso_matches)!=2:raise StorageError('invalid_date','Too many explicit date endpoints')
        try:start,end=[date(*map(int,m.groups())) for m in iso_matches]
        except ValueError:raise StorageError('invalid_date','Invalid explicit interval') from None
        if start>end:raise StorageError('invalid_date','Reversed explicit date interval')
        return result(start,end,'explicit_period_range')
    if iso:
        try:d=date(*map(int,iso.groups()))
        except ValueError:raise StorageError('invalid_date','Invalid explicit ISO date') from None
        return result(d,d,'explicit_date')
    numeric=re.search(r'(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?!\d)',t)
    if numeric and numeric[3] is None and t[numeric.end():].startswith('.'):
        return unknown('unsupported_date_year')
    years=set(YEAR_RE.findall(t))
    if numeric and numeric[3]:years.add(numeric[3])
    if len(years)>1:
        month_matches=list(MONTH_RE.finditer(t))
        if len(month_matches)==2:
            endpoints=[]
            for i,m in enumerate(month_matches):
                segment=t[m.end():month_matches[i+1].start() if i+1<len(month_matches) else len(t)]
                local_years=YEAR_RE.findall(segment)
                if len(local_years)!=1:break
                y=int(local_years[0]);month=MONTHS[m[0][:3]]
                preceding=re.search(r'(\d{1,2})\s*$',t[:m.start()])
                try:point=date(y,month,int(preceding[1])) if preceding else _end(y,month) if i==1 else date(y,month,1)
                except ValueError:raise StorageError('invalid_date','Invalid range endpoint') from None
                endpoints.append(point)
            if len(endpoints)==2:
                if endpoints[0]>endpoints[1]:raise StorageError('invalid_date','Reversed month range')
                return result(*endpoints,rule='explicit_period_range')
        return unknown('ambiguous_period_year')
    year=int(next(iter(years))) if years else None
    year_rule='explicit_year'
    context_years=set(re.findall(r'\b((?:19|20)\d{2})\s*г(?:од\w*|\.)',context.casefold()))
    if year is None and len(context_years)==1:
        year=int(next(iter(context_years)));year_rule='context_year'
    relative=re.search(r'\b(?:этом|текущем|прошлом|следующем)\s+году\b',t)
    if relative:
        if published is None:return unknown('publication_anchor_missing')
        delta=-1 if 'прошлом' in relative[0] else 1 if 'следующем' in relative[0] else 0
        relative_year=published.year+delta
        if years and year is not None and year!=relative_year:
            raise StorageError('contradictory_period','Explicit year conflicts with relative year')
        year=relative_year;year_rule='relative_publication_year';refs.append('/article/published_at')
    if re.search(r'(?:до|на)\s+кон(?:ец|ц)\w*\s+года',t):
        if year is None:
            if published is None:return unknown('publication_anchor_missing')
            year=published.year;refs.append('/article/published_at');year_rule='relative_publication_year'
        d=date(year,12,31)
        return result(d if t.startswith('на ') else None,d,year_rule+'_year_end')
    if re.search(r'с\s+начала\s+(?:(?:19|20)\d{2}\s+)?года',t):
        if year is None:
            if published is None:return unknown('publication_anchor_missing')
            year=published.year;refs.append('/article/published_at');year_rule='relative_publication_year'
        return result(date(year,1,1),None,year_rule+'_year_start',['observation_cutoff_unknown'])
    months=list(MONTH_RE.finditer(t))
    shape=None
    if numeric:
        day,month=int(numeric[1]),int(numeric[2]);shape=lambda y:(date(y,month,day),date(y,month,day))
    elif months:
        def endpoint(match,y,last=False):
            month=MONTHS[match[0][:3]]
            preceding=re.search(r'(\d{1,2})\s*$',t[:match.start()])
            if preceding:return date(y,month,int(preceding[1]))
            return _end(y,month) if last else date(y,month,1)
        shape=lambda y:(endpoint(months[0],y),endpoint(months[-1],y,True))
        if len(months)==1:
            def single(y):
                a=endpoint(months[0],y)
                if re.search(r'(\d{1,2})\s*$',t[:months[0].start()]):return a,a
                return a,_end(y,a.month)
            shape=single
    elif 'квартал' in t:
        match=re.search(r'\b([1-9]|iv|iii|ii|i)\s*квартал',t)
        quarter=int(match[1]) if match and match[1].isdigit() else {'i':1,'ii':2,'iii':3,'iv':4}.get(match[1]) if match else None
        if quarter is None:
            quarter=next((n for stem,n in [('перв',1),('втор',2),('трет',3),('четв',4)] if stem in t),None)
        if quarter not in (1,2,3,4):raise StorageError('invalid_date','Invalid or unsupported quarter')
        shape=lambda y:(date(y,(quarter-1)*3+1,1),_end(y,quarter*3))
    elif 'полугоди' in t:
        half=1 if re.search(r'перв|\b(?:1|i)\s+полуг',t) else 2 if re.search(r'втор|\b(?:2|ii)\s+полуг',t) else None
        if half is None:return unknown()
        shape=lambda y:(date(y,1 if half==1 else 7,1),_end(y,6 if half==1 else 12))
    elif 'месяц' in t or 'месяцев' in t:
        count=re.search(r'\b(\d{1,2})\s+месяц',t)
        n=int(count[1]) if count else next((n for stem,n in [('девят',9),('трех',3),('трёх',3),('шест',6),('двенадцат',12)] if stem in t),None)
        if n is not None:
            if not 1<=n<=12:raise StorageError('invalid_date','Invalid accumulated month count')
            shape=lambda y:(date(y,1,1),_end(y,n))
    if shape is None:
        simple_year=re.fullmatch(r'(?:(?:в|во|за|на|по итогам|за весь|в течение)\s+)?(?:(?:19|20)\d{2}\s*(?:год\w*|г\.)?|(?:этом|текущем|прошлом|следующем)\s+году)',t)
        if year is not None and simple_year and (years or relative):
            warnings=['calendar_year_not_complete'] if published and year==published.year and published<date(year,12,31) else []
            return result(date(year,1,1),date(year,12,31),year_rule,warnings)
        return unknown()
    if year is not None:
        try:start,end=shape(year)
        except ValueError:raise StorageError('invalid_date','Invalid calendar date') from None
        if start>end:raise StorageError('invalid_date','Period ends before it starts')
        return result(start,end,year_rule)
    # A leap-day expression is valid even if no leap year falls inside the
    # configured +/- one-year search window. An impossible day is not.
    try:probe_start,probe_end=shape(2000)
    except ValueError:raise StorageError('invalid_date','Impossible calendar day') from None
    if probe_start>probe_end:raise StorageError('invalid_date','Reversed calendar interval')
    if published is None:return unknown('publication_anchor_missing')
    candidates=[]
    for y in (published.year-1,published.year,published.year+1):
        try:start,end=shape(y)
        except ValueError:continue
        if start>end:continue
        distance=0 if start<=published<=end else min(abs((published-start).days),abs((published-end).days))
        candidates.append((distance,start,end))
    if not candidates:return unknown('nearest_period_year_unavailable')
    candidates.sort()
    if len(candidates)>1 and candidates[0][0]==candidates[1][0]:return unknown('nearest_period_year_tie')
    refs.append('/article/published_at')
    return result(candidates[0][1],candidates[0][2],'nearest_period_year',['nearest_period_year'])
