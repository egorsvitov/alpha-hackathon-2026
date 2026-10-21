"""Human-authored B4 regression examples. Expected results never call normalizer."""
import json
from copy import deepcopy
from pathlib import Path
from llm.events.models import ArticleInput
from llm.events.validation import article_sha256


def measurement(value,unit,*,kind='level',role='reported',metric='ключевая ставка',qualifier='exact',comparison=None):
    return {'measurement_id':'m1','metric':metric,'kind':kind,'value_text':value,'unit_text':unit,
        'qualifier':qualifier,'value_role':role,'comparison_period':{'text':comparison} if comparison else None}


def claim(text,measurements=(),*,period=None,modality='asserted',entity=None,scenario=None):
    time={'occurred_at':None,'effective_at':None,'observation_period':{'text':period} if period else None,'forecast_horizon':None}
    result={'claim_id':'c1','text':text,'event_type':'policy_rate','modality':modality,'polarity':'affirmed',
        'stage':None,'attribution':None,'participants':[{'name':entity,'role':'authority'}] if entity else [],
        'action':None,'object':None,'geographies':[],'scenario':scenario,'time':time,
        'measurements':[dict(m,measurement_id=f'm{i+1}') for i,m in enumerate(measurements)],'evidence_ids':['e1'],'field_evidence':{}}
    for field in ('text','modality','polarity','participants','scenario','measurements','time'):
        if result[field] is not None and result[field]!=[] and (field!='time' or any(time.values())):
            result['field_evidence']['/'+field]=['e1']
    return result


def case(name,text,measurements=(),*,expected,period=None,published='2026-10-20T12:00:00+03:00',entity=None,modality='asserted'):
    article={'article_id':'b4:'+name,'source':'Synthetic B4','url':None,'title':'Вымышленный пример '+name,
        'published_at':published,'language':'ru','text':text,'content_kind':'synthetic','is_full_original_text':False,'is_synthetic':True}
    record={'source_id':'b4:'+name+':v1','article_version':1,'article':article}
    response={'claims':[claim(text,measurements,period=period,entity=entity,modality=modality)],
        'evidence':[{'evidence_id':'e1','field':'text','quote':text}],'excluded':[],'limitations':[]}
    return {'name':name,'article':record,'input':{'schema_version':'b3.1','source_id':record['source_id'],
        'article_sha256':article_sha256(ArticleInput.model_validate(article)),'processing_spec_hash':'fixture:hand-authored',
        'verification_status':'unverified','extraction':response},'expected':expected}


def cases():
    items=[]
    def add(*args,**kwargs):items.append(case(*args,**kwargs))
    add('percent','Ключевая ставка составляет 14%.',[measurement('14','%')],expected={'values':['14'],'units':['percent'],'quality':'complete'})
    add('growth','Выручка выросла на 12%.',[measurement('12','%',kind='relative_change',metric='выручка')],expected={'values':['12']})
    add('forecast_decline','Компания ожидает снижения спроса на 0,5%.',[measurement('0,5','%',kind='relative_change',metric='спрос')],modality='forecast',expected={'values':['-0.5']})
    add('forecast_revision','Прогноз падения спроса улучшен с 7% до 0,5%.',[
        measurement('7','%',kind='relative_change',role='before',metric='спрос'),measurement('0,5','%',kind='relative_change',role='after',metric='спрос')],modality='forecast',expected={'values':['-7','-0.5']})
    add('investment_revision','Газпром сократил финансирование с 1,1 трлн до 1,096 трлн рублей.',[
        measurement('1,1','трлн рублей',role='before',metric='финансирование'),measurement('1,096','трлн рублей',role='after',metric='финансирование')],entity='Газпром',expected={'values':['1100000000000','1096000000000'],'units':['RUB','RUB']})
    add('range','Доход прогнозируется в диапазоне 185–190 млрд рублей.',[measurement('185–190','млрд рублей',metric='доход')],expected={'values':[{'min':'185000000000','max':'190000000000'}]})
    add('points','Доля снизилась на 0,5 процентного пункта.',[measurement('0,5','процентного пункта',kind='absolute_change',metric='доля')],expected={'values':['-0.5'],'units':['percentage_point']})
    add('basis_points','Ставка снизилась на 25 базисных пунктов.',[measurement('25','базисных пунктов',kind='absolute_change')],expected={'values':['-25'],'units':['basis_point']})
    add('kopecks','Стоимость составляет 3,75 копейки.',[measurement('3,75','копейки',metric='стоимость')],expected={'values':['0.0375'],'units':['RUB']})
    add('oil_bases','В сентябре 2026 года поставки — около 2,42 млн баррелей в сутки, на 20% больше, чем годом ранее, но на 8,5% меньше, чем в августе.',[
        measurement('2,42','млн баррелей в сутки',metric='поставки',qualifier='approximate'),measurement('20','%',kind='relative_change',metric='поставки',comparison='годом ранее'),measurement('8,5','%',kind='relative_change',metric='поставки',comparison='в августе')],period='В сентябре 2026 года',expected={'values':['2420000','20','-8.5']})
    add('unicode','😀 Ключевая ставка — 14\u00a0%.',[measurement('14','%')],expected={'values':['14']})
    add('past_september','В сентябре опубликован показатель.',period='В сентябре',expected={'period_start':'2026-09-01','warning':'nearest_period_year'})
    add('future_september','В сентябре ожидается показатель.',period='В сентябре',published='2026-08-15T12:00:00+03:00',modality='forecast',expected={'period_start':'2026-09-01','warning':'nearest_period_year'})
    add('next_january','В январе ожидается показатель.',period='В январе',published='2026-12-27T12:00:00+03:00',expected={'period_start':'2027-01-01','warning':'nearest_period_year'})
    add('leap','На 29 февраля приведён показатель.',period='29 февраля',published='2025-03-01T12:00:00+03:00',expected={'period_start':'2024-02-29','warning':'nearest_period_year'})
    add('repeated','Ключевая ставка — 14%. Ключевая ставка — 14%.',[measurement('14','%')],expected={'rejected':'quote_ambiguous'})
    items[-1]['input']['extraction']['evidence'][0]['quote']='Ключевая ставка — 14%.'
    add('missing_quote','Ключевая ставка — 14%.',[measurement('14','%')],expected={'rejected':'quote_not_found'})
    items[-1]['input']['extraction']['evidence'][0]['quote']='Ключевая ставка — 15%.'
    add('uncovered','Ключевая ставка — 14%.',[measurement('14','%')],expected={'rejected':'uncovered_field'})
    del items[-1]['input']['extraction']['claims'][0]['field_evidence']['/modality']
    add('ambiguous_cb','ЦБ сохранил ключевую ставку 14%.',[measurement('14','%')],entity='ЦБ',expected={'entity_id':None,'warning':'entity_unresolved'})
    add('ambiguous_direction','Показатель составил 12%.',[measurement('12','%',kind='relative_change')],expected={'rejected':'ambiguous_direction'})
    add('zero','Общая описательная фраза.',expected={'quality':'complete','accepted':0})
    items[-1]['input']['extraction']['claims']=[];items[-1]['input']['extraction']['evidence']=[]
    add('partial','Ключевая ставка — 14%.',[measurement('14','%')],expected={'quality':'partial','accepted':1,'rejected':'uncovered_field'})
    bad=deepcopy(items[-1]['input']['extraction']['claims'][0]);bad['claim_id']='c2';bad['measurements']=[]
    del bad['field_evidence']['/modality'];items[-1]['input']['extraction']['claims'].append(bad)
    return items


if __name__=='__main__':
    Path(__file__).with_name('normalization.b4.1.json').write_text(json.dumps(cases(),ensure_ascii=False,indent=2)+'\n')
