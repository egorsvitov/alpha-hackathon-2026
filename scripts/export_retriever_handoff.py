"""Export exact showcase batches and matching profiles; no LLM calls or adapters."""
import argparse
import csv
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from ranking.contracts import canonical_json,payload_hash
from ranking.profiles import file_hash
from retriever.contracts import validate_candidate_batch
from retriever.fusion import estimate_tokens
from retriever.index import EventIndex

PRODUCTS={'rko':'РКО','loan':'кредит','leasing':'лизинг','deposit':'депозит','guarantee':'гарантии','payroll':'зарплатный проект','factoring':'факторинг'}

def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    source=args.source;out=args.output
    if out.exists() or out.with_suffix('.zip').exists():raise FileExistsError('Choose a new export directory')
    original=json.loads((source/'manifest.json').read_text());index=EventIndex.open('data/processed/retriever/company-embeddings/event-index')
    assert index.index_snapshot_id==original['index_snapshot_id']
    out.mkdir(parents=True)
    for name in ('analyst-inputs','user-content','companies','schemas'):(out/name).mkdir()
    rows=[];profiles=[];entries=[];unique_events=set()
    for company in original['selected_companies']:
        uid=company['company_id'];result_path=source/'results'/f'{uid}.json';profile_path=source/'profiles'/f'{uid}.json'
        result=json.loads(result_path.read_text());profile=json.loads(profile_path.read_text());batch=result['candidate_batch']
        assert batch['request']['company_id']==profile['company_id']==uid
        validate_candidate_batch(batch,profile=profile,index=index)
        assert estimate_tokens(batch)==result['trace']['token_estimate']
        dump(out/'analyst-inputs'/f'{uid}.json',batch)
        (out/'user-content'/f'{uid}.txt').write_text(canonical_json(batch),encoding='utf-8')
        shutil.copyfile(profile_path,out/'companies'/f'{uid}.json')
        assert json.loads((out/'user-content'/f'{uid}.txt').read_text())==batch
        assert json.loads((out/'analyst-inputs'/f'{uid}.json').read_text())==result['candidate_batch']
        profile_hash=payload_hash(profile)
        assert profile_hash==batch['company_context']['profile_hash']
        events=[]
        for candidate in batch['candidates']:
            for member in candidate['members']:
                unique_events.add(member['snapshot']['event_id'])
                assert payload_hash(member['bundle'])==member['snapshot']['payload_sha256']
                events.append(dict(event_id=member['snapshot']['event_id'],title=member['bundle']['event']['title'],payload_sha256=member['snapshot']['payload_sha256']))
        entries.append(dict(**company,profile_version=profile['profile_version'],profile_hash=profile_hash,
            candidate_batch_path=f'analyst-inputs/{uid}.json',user_content_path=f'user-content/{uid}.txt',profile_path=f'companies/{uid}.json',
            source_result_sha256=file_hash(result_path),candidate_batch_sha256=payload_hash(batch),candidate_count=len(batch['candidates']),events=events,
            input_estimate=estimate_tokens(batch),counter_version='utf8_bytes_div4.v1',max_retrieval_input_tokens=batch['request']['max_input_tokens']))
        products=[PRODUCTS.get(k,k) for k,v in profile['products'].items() if v is True]
        rows.append(dict(company_id=uid,sector=company['sector'],region=profile['region'],city=profile['city'],okved_code=profile['okved']['code'],okved_name=profile['okved']['name'],
            observed_products='; '.join(products),fts_present=profile['fts_present'],countries='; '.join(profile['countries']),profile_version=profile['profile_version']))
        profiles.append(profile)
    dump(out/'companies.json',profiles)
    with (out/'companies.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    shutil.copyfile('retriever/contracts/impact-candidate-batch.v1.schema.json',out/'schemas/impact-candidate-batch.v1.schema.json')
    shutil.copyfile('docs/contracts/event-bundle.v0.2.schema.json',out/'schemas/event-bundle.v0.2.schema.json')
    manifest=dict(export_version='analyst-handoff-export.v1',created_at=datetime.now(timezone.utc).isoformat(),
        status='validated_handoff_data_not_live_api_request',source_showcase=str(source),source_manifest_sha256=file_hash(source/'manifest.json'),
        index_snapshot_id=index.index_snapshot_id,company_snapshot_id=original['company_snapshot_id'],encoder_fingerprint=index.fingerprint,
        company_source_sha256=original['company_source_sha256'],corpus_sha256=original['corpus_sha256'],role='cfo',as_of=index.as_of,
        company_count=len(entries),candidate_occurrences=sum(e['candidate_count'] for e in entries),distinct_events=len(unique_events),
        graph_enabled=False,bank_rag_status='not_retrieved',provider_request_status='not_configured',live_calls=0,
        integration_status='legacy_context_expects_Relevance; R7_batch_context_adapter_pending',companies=entries)
    dump(out/'manifest.json',manifest)
    docs=['# Пакеты для LLM-аналитика: восемь компаний','',
          '**Это точный экспорт данных ретривера из теста, а не результат LLM и не готовый HTTP-запрос к провайдеру.** Все 8 пакетов прошли schema/semantic validation. В каждом 6 событий; 48 вхождений, 22 разных события. Порядок и содержимое выдачи не менялись.','',
          '## Что открывать','',
          '- `analyst-inputs/<company_id>.json` — читаемый JSON `ImpactCandidateBatch v1`: компания и выбранные события. Это данные на новом стыке ретривер → аналитик.',
          '- `user-content/<company_id>.txt` — ровно тот же JSON, сериализованный компактно. Подготовленный текст для содержимого user-сообщения после подключения нового адаптера. В нём нет обёртки provider/model/messages и системного промпта.',
          '- `companies/<company_id>.json` — полный исходный структурированный профиль конкретной компании для проверки признаков и refs.',
          '- `companies.json` — все восемь полных профилей; `companies.csv` — удобная сводная таблица (UTF-8 BOM).',
          '- `manifest.json` — ID, пути, версии/hashes, названия выбранных событий и оценки объёма.',
          '- `schemas/` — схемы верхнего batch и вложенного B6 v0.2.',
          '- `CHECKSUMS.json` — SHA-256 всех остальных файлов экспорта.','',
          'В модель передаётся отдельный пакет одной компании, а не вся папка и не `companies.json` целиком. `company_context` уже содержит отобранные факты о ней. Полный профиль отдельно нужен для аудита/разрешения ссылок; его автоматическое дублирование в prompt в текущий budget не входит.','',
          '## Структура данных','',
          '```text',
          'ImpactCandidateBatch v1',
          '  request: company_id, role=cfo, as_of, window, лимиты',
          '  company_context: версия/hash профиля, verified_facts, неизвестные данные',
          '  candidates[6]:',
          '    candidate_id',
          '    members[1]:',
          '      snapshot: ID/версия события, payload hash, receipt',
          '      bundle: исходный B6 v0.2',
          '        event / ranking_features / claims / sources / evidence / conflicts / relations / limitations',
          '    match_context: каналы, совпавшие views, feature_ids, claim_refs, event_paths',
          '    limitations',
          '  retrieval_context: trace_id, index_snapshot_id, encoder_fingerprint, версии',
          '  limitations',
          '```','',
          'B6 и все claims/evidence/conflicts сохранены целиком. Ссылки на первоисточники находятся в `candidates[].members[].bundle.sources`; текст фактов — в `.claims[].text`. Численные cosine/BM25/RRF и векторы в эти пакеты не добавлялись: они остаются в trace исходного теста. Позиция в candidates — порядок отбора, не доказанная значимость для бизнеса.','',
          '## Участники теста','', '| ID | Сфера | Регион | Пакет | Профиль | Оценка входа |','|---|---|---|---|---|---:|']
    for e in entries:
        uid=e['company_id'];docs.append(f"| {uid} | {e['sector']} | {e['region']} | [JSON]({e['candidate_batch_path']}) · [user content]({e['user_content_path']}) | [профиль]({e['profile_path']}) | {e['input_estimate']} / 8192 |")
    docs+=['','Компании обезличены: ID/сфера — подписи для теста, не выдуманные юридические наименования. Регион — регион регистрации, а не полный список операционных площадок. Пустое поле CSV не означает false или отсутствие связи: точные value/quality/origin смотрите в JSON.','',
           '## Что ещё не подключено','',
           '- Текущий `llm/agent/context.py::build_context` принимает один snapshot, полный профиль и старый `Relevance`; новый batch напрямую он пока не принимает. `llm/agent/orchestrator.py` разрешает только fixture generator. Поэтому эти файлы **подготовлены для нового входа**, а не объявлены уже отправляемым production API payload.',
           '- Системная инструкция, модель, response schema и provider-specific HTTP-обёртка в этом экспорте не фиксируются. Их должен добавить R7/gateway после согласования контракта нового аналитика; старый Relevance не подделывался.',
           '- Bank RAG здесь **не запускался**: нельзя делать вывод «аналитики банка нет» или приписывать банку позицию. Этот контекст должен поступить отдельным проверяемым результатом поиска.',
           '- Граф выключен. В `payment_context` статус `not_loaded`: это не утверждение, что у компании нет контрагентов.',
           '- Пакеты содержат прежние сомнительные попадания, в том числе Getty Images: это точный вход для проверки аналитика. Аналитик должен иметь возможность отклонить событие, а не обязательно выдумывать влияние каждого.',
           '- `source_mode=fixture` означает воспроизводимый локальный replay. Источники новостей остаются реальными; корпус quote-only, часть текстов — RSS-анонсы. Это не ручная разметка релевантности.',
           '- 8192 и числа в таблице — оценка UTF-8 bytes/4 **только компактного batch**. Системный prompt, RAG, HTTP messages и ответ не учтены. Pretty JSON занимает больше символов. Перед реальным вызовом нужен tokenizer выбранной модели и пересчёт полного бюджета; никаких вызовов API при экспорте не было.','',
           '## Воспроизведение','', '```bash',f'.venv/bin/python scripts/export_retriever_handoff.py --source {source} --output <новая_папка>', '```','']
    (out/'README.md').write_text('\n'.join(docs),encoding='utf-8')
    checksums={str(p.relative_to(out)):file_hash(p) for p in sorted(out.rglob('*')) if p.is_file()}
    dump(out/'CHECKSUMS.json',checksums)
    archive=out.with_suffix('.zip')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob('*')):
            if p.is_file():z.write(p,Path(out.name)/p.relative_to(out))
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    assert all(file_hash(out/path)==sha for path,sha in checksums.items())
    print(json.dumps({'companies':len(entries),'candidate_occurrences':manifest['candidate_occurrences'],'distinct_events':len(unique_events),'validation':'passed','output':str(out),'zip':str(archive)},ensure_ascii=False))

if __name__=='__main__':main()
