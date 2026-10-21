"""Independent hand-authored B5 examples. Expected labels do not call the matcher."""
from copy import deepcopy
import json
import re
from pathlib import Path

TYPES=['policy_rate','fx_rate','price_change','regulation','sanction_or_trade_restriction',
       'market_indicator','company_results','investment','corporate_action','operational_incident','other']


def context(cid,event_type='policy_rate',*,value='14',day='2026-10-23',forecast=False,release='F1',author='org:sinara_bank'):
    source_id='fixture:'+cid
    obj={'fx_rate':'USD/RUB','price_change':'oil_brent','regulation':'customs_rule',
         'sanction_or_trade_restriction':'oil_route_ru_cn','market_indicator':'steel_consumption',
         'company_results':'net_profit_ras','investment':'project_1','corporate_action':'asset_1',
         'operational_incident':'incident_1','other':'object_1'}.get(event_type,'policy_rate')
    text=f'Вымышленный Банк России: {obj}, значение {value} percent. Дата {day}, период сентябрь 2026 года, Россия, утверждено.'
    if forecast:text+=f' Прогноз №{release}, автор {author}, горизонт 2026 год.'
    time={'occurred_at':{'start':day,'end':day,'text':day},
          'effective_at':{'start':day,'end':day,'text':day},
          'observation_period':{'start':'2026-09-01','end':'2026-09-30','text':'сентябрь 2026 года'},
          'forecast_horizon':{'start':'2026-01-01','end':'2026-12-31','text':'2026 год'} if forecast else None}
    claim={'claim_id':cid,'text':text,'modality':'forecast' if forecast else 'asserted','polarity':'affirmed','stage':None if forecast else 'approved',
        'attribution':{'entity_id':author,'name':author} if forecast else None,
        'participants':[{'entity_id':'org:bank_of_russia','name':'Банк России','role':'authority'}],
        'action':'keep','object':obj,'geographies':['RU'],'time':time,'scenario':None,
        'measurements':[{'measurement_id':cid+':m','metric':'official_exchange_rate' if event_type=='fx_rate' else obj,'kind':'level','value':value,'unit':'percent',
                         'qualifier':'exact','comparison_period':None,'value_role':'reported'}],
        'evidence_ids':[cid+':e'],'field_evidence':{}}
    for name,value_ in claim.items():
        if name not in ('claim_id','evidence_ids','field_evidence') and value_ is not None and value_!=[]:
            claim['field_evidence']['/'+name]=[cid+':e']
    return {'artifact_id':'artifact:'+cid,'candidate_id':cid,'event_type':event_type,'claim':claim,
        'source':{'source_id':source_id,'article_id':'article:'+cid,'article_version':1,'title':'Вымышленный пример',
                  'publisher':'Synthetic','url':None,'published_at':'2026-10-20T12:00:00+03:00',
                  'content_kind':'synthetic','is_full_original_text':False,'is_synthetic':True},
        'evidence':[{'evidence_id':cid+':e','source_id':source_id,'field':'text','start':0,'end':len(text),'quote':text}],
        'limitations':[],'diagnostics':[],'transformations':[]}


def cases():
    cases=[]
    def add(name,a,b,identity,content='none'):
        cases.append({'name':name,'contexts':[a,b],'expected_pairs':[{'pair':[a['claim']['claim_id'],b['claim']['claim_id']],
            'identity':identity,'content':content}],
            'expected_groups':[[a['claim']['claim_id'],b['claim']['claim_id']]] if identity=='same_event' else [[a['claim']['claim_id']],[b['claim']['claim_id']]]})
    for event_type in TYPES:
        add(event_type+':conflict',context('a',event_type),context('b',event_type,value='15'),'same_event','conflicts_with')
        a,b=context('a',event_type),context('b',event_type);b['claim']['object']=None;b['claim']['field_evidence'].pop('/object')
        add(event_type+':missing',a,b,'uncertain')
    add('duplicate',context('a'),context('b'),'same_event','duplicate_claim')
    add('different_meetings',context('a'),context('b',day='2026-09-18'),'different_event')
    add('forecast_vs_decision',context('a'),context('b',forecast=True),'different_event')
    add('forecast_authors',context('a',forecast=True),context('b',forecast=True,author='org:other'),'different_event')
    add('forecast_releases',context('a',forecast=True),context('b',forecast=True,release='F2'),'different_event')
    a,b=context('a',forecast=True),context('b',forecast=True)
    for x in (a,b):x['evidence'][0]['quote']=x['evidence'][0]['quote'].replace('Прогноз №F1','Ожидание');x['evidence'][0]['end']=len(x['evidence'][0]['quote'])
    add('unknown_release',a,b,'uncertain')
    a,b=context('a',forecast=True,value='13.75'),context('b',forecast=True,value='14')
    for x,kind in ((a,'baseline'),(b,'alternative')):
        x['claim']['scenario']={'kind':kind,'text':kind};x['claim']['field_evidence']['/scenario']=[x['claim']['claim_id']+':e']
    add('scenarios',a,b,'same_event','complements')
    a,b=context('a'),context('b',value='15');b['claim']['measurements'][0]['qualifier']='approximate'
    add('approximate',a,b,'same_event','complements')
    a,b=context('a'),context('b',value='15');b['claim']['measurements'][0]['value_role']='before'
    add('before_after',a,b,'same_event','complements')
    a,b=context('a'),context('b');b['claim']['measurements'][0]['comparison_period']={'start':'2025-01-01','end':'2025-12-31','text':'2025 год'}
    add('comparison_bases',a,b,'same_event','complements')
    a,b=context('a'),context('b');b['claim']['stage']='effective'
    add('stages',a,b,'different_event')
    for item in cases:
        articles=[]
        for entry in item['contexts']:
            c=entry['claim'];release=re.search(r'Прогноз №[\w.-]+',entry['evidence'][0]['quote'])
            # A literal synthetic record containing the explicitly authored
            # proposition, not an output generated by the matching algorithm.
            semantic={k:v for k,v in c.items() if k not in ('claim_id','text','evidence_ids','field_evidence')}
            quote='Вымышленное сообщение. '+(release.group()+'; ' if release else '')+json.dumps(semantic,ensure_ascii=False)
            c['text']=quote;entry['evidence'][0].update(quote=quote,start=0,end=len(quote))
            source=entry['source']
            articles.append({'source_id':source['source_id'],'article_version':source['article_version'],'article':{
                'article_id':source['article_id'],'source':source['publisher'],'url':source['url'],'title':source['title'],
                'published_at':source['published_at'],'language':'ru','text':quote,'content_kind':'synthetic',
                'is_full_original_text':False,'is_synthetic':True}})
        item['articles']=articles
    return {'schema_version':'b5.1','annotation_status':'single_annotator_draft','cases':cases}


if __name__=='__main__':
    Path(__file__).with_name('catalog.b5.1.json').write_text(json.dumps(cases(),ensure_ascii=False,indent=2)+'\n')
