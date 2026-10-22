"""Reproduce a readable showcase with the unchanged production retriever."""
from contextlib import closing
from pathlib import Path
from collections import Counter
import argparse
import html
import json
import time
from datetime import datetime, timezone
from ranking.profiles import CompanyRepository, file_hash
from ranking.contracts import payload_hash
from retriever.company_build import CompanyEmbeddingSnapshot
from retriever.embedding_cache import VectorCache
from retriever.company_queries import load_company_queries
from retriever.index import EventIndex
from retriever.representation_contracts import QueryPolicy, VariantConfig
from retriever.service import RetrievalService

ARTIFACTS=Path('data/processed/retriever/company-embeddings')
CORPUS=Path('ranking/experiments/cpu_news_embeddings/artifacts/events.v0.2.jsonl')
SECTORS=[('Перевозки','49.4'),('IT','62.'),('Строительство','41.'),('Пищевая промышленность','10.'),
         ('Сельское хозяйство','01.'),('Оптовая торговля','46.'),('Гостиницы','55.'),('Медицина','86.')]
PRODUCTS={'rko':'РКО','loan':'кредит','leasing':'лизинг','deposit':'депозит','guarantee':'гарантии','payroll':'зарплатный проект','factoring':'факторинг'}
VIEWS={'business':'деятельность','finance':'финансы','trade_geo':'регион / внешняя торговля'}

def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def select(repo):
    chosen=[];regions=set()
    for sector,prefix in SECTORS:
        options=[(uid,row) for uid,(row,_) in repo.rows.items() if uid not in repo.conflicts and row['CL_TYPE']=='CLIENT'
                 and row['MAINOKVED_CODE'].startswith(prefix) and row['REGION'] and row['REGION'] not in regions]
        if not options:raise ValueError('No company with an unused region: '+sector)
        uid,row=min(options,key=lambda pair:pair[0]);regions.add(row['REGION']);chosen.append((sector,uid))
    return chosen

def describe(record):
    parts=[]
    for hit in record['hits']:
        if hit['channel']=='dense':parts.append(f"{VIEWS.get(hit['view_kind'],hit['view_kind'])}: место {hit['rank']}, cosine {hit['raw_score']:.3f}")
        elif hit['channel']=='lexical':parts.append('BM25: место '+str(hit['rank'])+'; слова: '+', '.join(hit['matched_terms']))
        else:parts.append('Структурное совпадение: '+', '.join(b['kind'] for b in hit.get('basis',[])))
    return '; '.join(parts)

def mdtext(text):return str(text).replace('|','\\|').replace('\n',' ')

def render(out,manifest,rows,index):
    md=['# Выдача ретривера для восьми компаний','',
        'Результат настоящего прогона, без ручной перестановки новостей и без LLM-аналитика. Компании выбраны по отрасли и региону до просмотра выдачи.','',
        f"Корпус: **{len(index.snapshots)} новостей**, роль **CFO**, cutoff `{index.as_of}`, окно `{index.window_start}` — `{index.window_end}`. Базовый H3: dense + BM25 + structured, граф выключен. Бюджет 8192 (оценка UTF-8 bytes/4), максимум 20 событий.",'',
        'Здесь показаны **кандидаты для анализа**, не подтверждённые полезные новости. Cosine/RRF — технические величины, не вероятность пользы. Названия компаний обезличены; регион означает регистрацию.','',
        'События исходного корпуса имеют quote-only упаковку: многие содержат RSS-анонс, семантические поля unknown. Реального entity→company mapping нет.','',
        '## Компании','', '| ID | Сфера | Регион | Выдано | Поиск, мс |','|---|---|---|---:|---:|']
    sections=[]
    for row in rows:
        p=row['profile'];result=row['result'];trace=result['trace'];batch=result['candidate_batch'];uid=p['company_id']
        md.append(f"| {uid} | {mdtext(row['sector'])} | {mdtext(p['region'])} | {trace['selected']} | {trace['duration_seconds']*1000:.1f} |")
    md+=['','Время поиска не включает открытие и проверку индексов; они загружены один раз.','']
    occurrences=Counter(c['members'][0]['snapshot']['event_id'] for row in rows for c in row['result']['candidate_batch']['candidates'])
    md+=['## Повторяющиеся новости','',f"Различных выбранных событий: **{len(occurrences)}** из 100. Всего мест в выдачах: **{sum(occurrences.values())}**.",'']
    for uid,count in occurrences.most_common(8):md.append(f"- {count}/8 компаний: {index.snapshots[uid].bundle['event']['title']}")
    for row in rows:
        p=row['profile'];result=row['result'];trace=result['trace'];batch=result['candidate_batch'];uid=p['company_id']
        products=', '.join(PRODUCTS.get(k,k) for k,v in p['products'].items() if v is True) or 'нет положительных отметок'
        profile_text=f"ОКВЭД {p['okved']['code']}: {p['okved']['name']}. Регион: {p['region']}. Продукты: {products}."
        md += ['',f"## {row['sector']} — {uid}",'',profile_text,'',f"Статус: **{batch['status']}**. Выбрано: **{trace['selected']}**; оценка входа: **{trace['token_estimate']} / 8192**.",'',
               f"[Полный результат и trace](results/{uid}.json) · [Профиль](profiles/{uid}.json)",'','### Тексты запросов компании','']
        queries=trace['company_queries']['queries']
        query_html=''
        for view in queries:
            text=view['metadata_ref']['text'];md.append(f"- **{VIEWS.get(view['kind'],view['kind'])}:** {text}")
            query_html+=f"<p><b>{html.escape(VIEWS.get(view['kind'],view['kind']))}:</b> {html.escape(text)}</p>"
        md+=['','### Передано дальше','']
        cards=[];selected_ids=set()
        for position,candidate in enumerate(batch['candidates'],1):
            member=candidate['members'][0];b=member['bundle'];eid=member['snapshot']['event_id'];selected_ids.add(eid)
            record=next(r for r in trace['events'] if r['event_id']==eid)
            fusion_rank=next(i for i,r in enumerate(trace['events'],1) if r['event_id']==eid)
            title=b['event']['title'];explanation=describe(record);claim_text='\n\n'.join(c['text'] for c in b['claims'])
            refs=[]
            for source in b['sources']:
                url=source.get('url') or ''
                if url.startswith(('https://','http://')):refs.append((source.get('publisher') or 'Источник',url))
            links=' · '.join(f'[{name}]({url})' for name,url in refs)
            md += [f"**{position}. {title}**",'',f"Место до бюджета: {fusion_rank}. {links}",'',
                   '> '+mdtext(claim_text[:400])+(' … [фрагмент; полный текст в HTML/JSON]' if len(claim_text)>400 else ''),'',
                   f"Основания retrieval: {explanation}",'']
            links_html=' · '.join(f'<a href="{html.escape(url,quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(name)}</a>' for name,url in refs)
            cards.append(f'<article><h3>{position}. {html.escape(title)}</h3><p class="muted">Место до бюджета: {fusion_rank} · {html.escape(b["event"].get("first_published_at") or "Дата публикации неизвестна")} · {links_html}</p><p class="claim">{html.escape(claim_text)}</p><details><summary>Как найдено</summary><p>{html.escape(explanation)}</p><p>RRF: {record["score"]:.6f}; значение не является оценкой бизнес-влияния.</p><code>{html.escape(eid)}</code></details></article>')
        omitted=[(i,r) for i,r in enumerate(trace['events'],1) if not r['selected']]
        counts=Counter(r['omission_reason'] for _,r in omitted)
        md+=['### Не вошло в итоговый пакет','',f"Причины исключения в trace: `{dict(counts)}`. Первые 10 кандидатов до сокращения:",'', '| Место до бюджета | Новость | Причина |','|---:|---|---|']
        omitted_html=''
        for rank,r in omitted[:10]:
            title=index.snapshots[r['event_id']].bundle['event']['title'];reason=r['omission_reason']
            md.append(f"| {rank} | {mdtext(title)} | {reason} |")
            omitted_html+=f'<li><b>#{rank}</b> {html.escape(title)} <span class="muted">— {html.escape(reason or "unknown")}</span></li>'
        sections.append(f'<section data-company="{uid}"><h2>{html.escape(row["sector"])} · {uid}</h2><p>{html.escape(profile_text)}</p><p><b>{trace["selected"]} событий</b> · {trace["token_estimate"]}/8192 по оценке токенов · {trace["duration_seconds"]*1000:.1f} мс</p><details><summary>Какими текстами искали</summary>{query_html}</details><p><a href="results/{uid}.json">Полный JSON + trace</a> · <a href="profiles/{uid}.json">Профиль</a></p>{"".join(cards)}<details><summary>Не вошли в пакет: первые 10</summary><ul>{omitted_html}</ul><p>{html.escape(str(dict(counts)))}</p></details></section>')
    md+=['','## Как воспроизвести','', '```bash',f'.venv/bin/python scripts/review_retriever_companies.py --output {out}', '```','',
         'Повторный прогон создавайте в новой папке. Скрипт не меняет ретривер, исходные новости, профили, параметры модели или правила ранжирования. Полные входы/выходы и hashes находятся рядом.']
    (out/'README.md').write_text('\n'.join(md)+'\n')
    options=''.join(f'<option value="{r["profile"]["company_id"]}">{html.escape(r["sector"])} — {html.escape(r["profile"]["region"])}</option>' for r in rows)
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Проверка ретривера — 8 компаний</title><style>
body{font:16px/1.6 system-ui,sans-serif;background:#f4f5f7;color:#17212b;max-width:1100px;margin:0 auto;padding:24px}header,section{background:white;border-radius:14px;padding:24px;margin-bottom:20px}h1{font-size:28px}h2{font-size:24px}h3{font-size:19px}select{font:inherit;max-width:100%;padding:12px;border:1px solid #aaa;border-radius:8px}article{border-top:1px solid #ddd;padding:10px 0 20px}.muted{color:#626a73;font-size:14px}.claim{white-space:pre-wrap}a{color:#0059aa}summary{cursor:pointer;font-weight:600}code{overflow-wrap:anywhere}details{margin:12px 0}section[hidden]{display:none}.notice{background:#fff3cf;border-radius:8px;padding:14px}
</style><header><h1>Что ретривер нашёл для восьми компаний</h1><p>100 сохранённых новостей · 8 отраслей · 8 регионов · H3 / CFO · без графа и LLM-аналитика</p><p class="notice">Это кандидаты, а не доказанная польза бизнесу. Выдача настоящая: новости не переставлялись вручную. Нерелевантные попадания тоже показаны. Бюджет аналитика сокращает список целыми событиями.</p><p><a href="README.md">Текстовый отчёт</a> · <a href="manifest.json">Параметры и hashes</a> · <a href="corpus.json">Все 100 B6</a></p><label>Компания: <select id="company"><option value="all">Показать все</option>OPTIONS</select></label></header>SECTIONS<script>
const select=document.querySelector('#company');function show(){document.querySelectorAll('section[data-company]').forEach(s=>s.hidden=select.value!=='all'&&s.dataset.company!==select.value);}select.addEventListener('change',show);select.selectedIndex=1;show();
</script></html>'''.replace('OPTIONS',options).replace('SECTIONS',''.join(sections))
    (out/'index.html').write_text(page)
    manifest['selected_event_frequencies']=dict(occurrences)
    save(out/'manifest.json',manifest)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();out=args.output
    out.mkdir(parents=True,exist_ok=False);(out/'results').mkdir();(out/'profiles').mkdir()
    started=time.perf_counter();repo=CompanyRepository('data/companies/df_Uid_data_hack.csv');selection=select(repo)
    index=EventIndex.open(ARTIFACTS/'event-index');bundles=[json.loads(line) for line in CORPUS.read_text().splitlines()]
    assert len(bundles)==len(index.snapshots)==100
    assert {b['event']['event_id']:payload_hash(b) for b in bundles}=={uid:s.payload_sha256 for uid,s in index.snapshots.items()}
    save(out/'corpus.json',bundles)
    times={k:getattr(index,k) for k in ('as_of','window_start','window_end')};rows=[]
    with closing(VectorCache(ARTIFACTS/'objects.sqlite')) as cache,closing(CompanyEmbeddingSnapshot.open(ARTIFACTS/'full',cache=cache)) as snapshot:
        assert snapshot.manifest['build_identity']['company_source_sha256']==repo.sha256
        service=RetrievalService(index);config=VariantConfig('H3',**times)
        for sector,uid in selection:
            profile=repo.get(uid);queries=load_company_queries(snapshot,company_id=uid,expected_encoder_fingerprint=index.fingerprint,policy=QueryPolicy(),as_of=index.as_of)
            result=service.run(dict(request_id='showcase:'+uid,company_id=uid,profile=profile,role='cfo',**times),company_queries=queries,config=config)
            save(out/'results'/f'{uid}.json',result);save(out/'profiles'/f'{uid}.json',profile)
            rows.append(dict(sector=sector,profile=profile,result=result))
            print(json.dumps({'sector':sector,'company_id':uid,'region':profile['region'],'selected':result['trace']['selected'],'ms':round(result['trace']['duration_seconds']*1000,2)},ensure_ascii=False),flush=True)
        manifest=dict(created_at=datetime.now(timezone.utc).isoformat(),company_source_sha256=repo.sha256,corpus_file=str(CORPUS),corpus_sha256=file_hash(CORPUS),event_count=len(index.snapshots),
            index_snapshot_id=index.index_snapshot_id,company_snapshot_id=snapshot.snapshot_id,encoder_fingerprint=index.fingerprint,config=config.to_dict(),
            selection_policy='lexicographically_first_CLIENT_per_OKVED_prefix_with_unused_nonempty_region; before_retrieval',
            selected_companies=[dict(company_id=r['profile']['company_id'],sector=r['sector'],region=r['profile']['region']) for r in rows],
            graph_enabled=False,paid_llm_calls=0,quality_status='not_evaluated',elapsed_seconds=time.perf_counter()-started)
    render(out,manifest,rows,index)
    print('Report:',out/'README.md',flush=True)

if __name__=='__main__':main()
