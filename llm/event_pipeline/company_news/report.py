"""Local source-bearing HTML and source-free aggregate report exports."""
from collections import Counter
from pathlib import Path
import json
import platform
import subprocess
import numpy as np
from .io import read, rows, save, save_rows, sha, require, load_plan, complete

ISSUES=[
    dict(id='Q01',severity='high',status='open',title='Навигация и пустые страницы проходят в новости',evidence='Ручная выборка содержит меню статистики, страницы без показателей, обрывки листинга и аннулированную новость.',theory='Сходство банковской лексики с finance view поднимает шаблонные страницы.',proposal='Версионированный content-kind/boilerplate фильтр на оригиналах с причинами исключения. Проверить отдельным challenger, не менять этот test.'),
    dict(id='Q02',severity='high',status='open',title='Регион подменяет деловую связь',evidence='В выдаче IT и недвижимости встречаются прилеты политиков и видео у гостиницы.',theory='trade_geo с названием города даёт высокий dense или BM25 rank; max RRF не различает основание.',proposal='Отдельно понизить вес geographic-only hit; требовать второй деловой путь. Регион регистрации не считать всей географией операций.'),
    dict(id='Q03',severity='high',status='open',title='Клиент банка смешивается с банком',evidence='Наличие РКО/депозита приводит к банковской отчетности и депозитам банков в ЦБ.',theory='Шаблон base3 описывает продукт, а статьи — похожие слова при иной роли участника.',proposal='Явный субъект/адресат нормы в механизмах, контрпримеры банк↔клиент; отдельный task-specific gold.'),
    dict(id='Q04',severity='high',status='measured',title='Reranker теряет полезные документы',evidence='Dev final recall по известным positives: 0.208 без резерва и 0.458 с резервом 4.',theory='Cross-encoder измеряет соответствие короткому запросу, не доказанную релевантность компании.',proposal='Резерв 4 выбран только на dev. Не называть score вероятностью; сохранять лучшие query paths и потери по стадиям.'),
    dict(id='Q05',severity='high',status='open',title='Разметка слишком разрежена для вывода о победителе',evidence='248 проверенных пар: 115 dev и 133 test; в test только один send.',theory='Пул первых результатов содержит много шума, полезные пары за его пределами могут отсутствовать в gold.',proposal='Независимая человеческая разметка более глубокого объединенного пула; дополнительный случайный аудит. Неразмеченные не отрицательные.'),
    dict(id='Q06',severity='medium',status='measured',title='Групповой отбор увеличил число карточек',evidence='G3: 122 уникальные карточки; G4: 133 при 20 компаниях.',theory='Дедупликация освободила места для других документов и создала новые точные source sets.',proposal='Не объявлять экономию по числу групп. Сравнить точные наборы и фактические LLM-токены; сейчас это подготовленные запросы.'),
    dict(id='D01',severity='high',status='unverified',title='Историческая доступность профилей не доказана',evidence='Источник компаний не содержит подтвержденного data_as_of.',theory='Дата новости сама по себе не делает эксперимент историческим backtest.',proposal='Использовать датированный экспорт профилей/графа; текущий результат — статическая диагностика. Граф не запускался.'),
    dict(id='D02',severity='medium',status='missing_input',title='Общий CPU-контроль из 344 новостей отсутствует',evidence='Найденный старый artifacts/articles.jsonl содержит 100 записей.',theory='Переданный контекст относится к другому локальному экспорту.',proposal='Получить точный manifest и document IDs от CPU-машины; не подменять контроль произвольными 344 статьями.'),
    dict(id='D03',severity='medium',status='missing_input',title='Исходный integration context не найден',evidence='В исходной ветке и проверенных ветках файл отсутствовал.',theory='Handoff ссылается на локальный файл коллеги.',proposal='Создан явный integration checkpoint. Сверить общий контракт коммитом 786a6e0, не считать checkpoint восстановленным исходником.'),
    dict(id='D04',severity='medium',status='measured',title='18 коротких новостей превышают токенный лимит E5',evidence='3012 BGE документов против 2994 E5; исходные тела не обрезались.',theory='Лимит в символах не гарантирует лимит в токенах, особенно в таблицах и числах.',proposal='Отдельная windowing policy с координатами как будущий challenger; сейчас явные encoder exclusions.'),
    dict(id='D05',severity='medium',status='open',title='Содержательная дата и timestamp страницы могут различаться',evidence='В исходниках встречаются прошлые даты событий и Last updated у страниц статистики.',theory='Дата обновления сайта может быть принята за публикацию новости.',proposal='Разделять observed_at, published_at и даты утверждений; не исправлять источник догадкой.'),
    dict(id='D06',severity='medium',status='preserved',title='Профили содержат неизвестные страны и конфликт записи',evidence='Один конфликт профиля исключен; UNKNOWN: токены сохранены.',theory='Нормализация справочника неполна; отсутствие значения не равно отрицанию.',proposal='Пополнить версионированный справочник с источниками; не выводить подтвержденную ВЭД-географию из unknown.'),
    dict(id='I01',severity='low',status='resolved',title='Первый download reranker завершился таймаутом',evidence='Ошибка сохранена в setup/reranker-download.log; повторный явный запуск прошел.',theory='Сетевой таймаут HF при чтении tokenizer.',proposal='Закрепленные small files сверены по Git/LFS identity; веса транспорта ModelScope сверены с официальным SHA-256.'),
    dict(id='I02',severity='low',status='resolved',title='Формат конфигурации pooling изменился',evidence='ST 5.7 сообщает pooling_mode=cls вместо прежнего pooling_mode_cls_token.',theory='Проверка ожидала старые имена полей.',proposal='Проверять фактический CLS и хэш полного model snapshot. Parity на 8 статьях: max abs error 1.49e-8.'),
    dict(id='I03',severity='low',status='resolved',title='Analyst имел зависимости вне своего каталога',evidence='Первые regression tests выявили недостающие ChangesSincePrevious, orchestrator и synthetic fixtures.',theory='Импорт только app/analyst недостаточен для тестов старого пути.',proposal='Перенесены проверенные зависимости pinned ef84f53; 276 legacy analyst tests проходят. Режим API не переключался.'),
    dict(id='I04',severity='medium',status='resolved',title='JSON Schema не проверял timezone без optional dependency',evidence='Ranking regression принял imported_at без timezone: rfc3339-validator отсутствовал. Также восстановлены недостающие pinned schemas/fixtures.',theory='jsonschema включает проверку date-time только при наличии соответствующей зависимости.',proposal='Зависимость закреплена в observed lock; все 36 ranking tests проходят. Не считать установку одного jsonschema достаточной проверкой дат.'),
    dict(id='A01',severity='high',status='awaiting_configuration',title='Новые live вызовы не запущены',evidence='Для этого эксперимента не предоставлены общий журнал, max-calls и бюджет.',theory='Старая квота прошлого GPU_HANDOFF не назначает бюджет новому эксперименту.',proposal='analyze-live требует существующий request journal и общий monetary store, подтвержденные профиль/тарифы и конечный cap. Квоты не создаются и не сбрасываются.'),
    dict(id='A02',severity='medium',status='explicit_limit',title='Ссылки и русский язык не доказывают смысловую поддержку',evidence='Raw adapter проверяет bounds/hash/references/language; semantic_support остается unreviewed.',theory='Корректная ссылка может сопровождать неподдержанный вывод.',proposal='Проверять утверждения по исходнику человеком/отдельным entailment протоколом; не превращать validation success в quality score.'),
    dict(id='A03',severity='medium',status='open',title='Граница бюджетных суток требует согласования',evidence='У существующего CallLedger календарные сутки Москвы; в инструкции организаторов указан сброс в 03:00 МСК.',theory='Локальный monetary journal сам по себе не доказывает остаток общей квоты провайдера, особенно около смены суток.',proposal='До live сверить общий остаток и политику журнала. Не создавать новый журнал ради нового дня; согласовать исправление общего runtime отдельным изменением.'),
]


def report(root):
    from sklearn.decomposition import PCA
    root=Path(root);m=load_plan(root)
    for stage in ('index','retrieve','rerank','select','analyze'):require(root,stage)
    docs=rows(root/'documents.jsonl');profiles=rows(root/'profiles.jsonl');queries=rows(root/'queries.jsonl')
    dev=read(root/'evaluation-dev.json');test=read(root/'evaluation-test.json');accounting=read(root/'analysis-accounting.json')
    index=read(root/'index.stage.json');retr=read(root/'retrieve.stage.json');rer=read(root/'rerank.stage.json')
    rankings=rows(root/'retrieval.jsonl')+rows(root/'reranking.jsonl');batches=rows(root/'selected.jsonl')
    rtimes=[r['seconds'] for r in rows(root/'reranking.jsonl')]
    if not (root/'reranker-download.json').exists():
        save(root/'reranker-download.json',read('.cache/company-news-models/BAAI--bge-reranker-v2-m3/verified.json'))
    try:
        gpu_info=subprocess.check_output(['nvidia-smi','--query-gpu=name,memory.total,driver_version','--format=csv,noheader'],stderr=subprocess.DEVNULL).decode().strip()
    except (OSError,subprocess.SubprocessError):
        gpu_info='unavailable on report host; see measured model/stage profiles'
    files={p.name:p.stat().st_size for p in root.iterdir() if p.is_file()}
    summary=dict(experiment_id=m['experiment_id'],inventory=read(root/'inventory.json'),models=m['models'],fusion=m['fusion'],budget=m['budget'],
        index=index['metrics'],performance=retr['metrics'],reranker=dict(rer['metrics'],per_company_p50_seconds=float(np.median(rtimes)),per_company_p95_seconds=float(np.percentile(rtimes,95)),profile=read(root/'reranker-profile.json')),
        download=read(root/'reranker-download.json'),
        peak_ram_bytes=dict(index=index['peak_process_ram_bytes'],retrieval=retr['peak_process_ram_bytes'],rerank=rer['peak_process_ram_bytes']),
        artifacts_bytes=sum(files.values()),cache_bytes={k:v for k,v in files.items() if k.endswith('.sqlite3')},
        evaluations={s:{k:v for k,v in value.items() if k not in ('per_company','misses')} for s,value in [('dev',dev),('test',test)]},
        lazy=accounting,limitations=test['limitations']+['control344_missing','no_new_live_calls','graph_not_run','static_not_historical'],
        validation=read(root/'verification.json') if (root/'verification.json').exists() else {'state':'see setup logs'},
        hardware=dict(platform=platform.platform(),python=platform.python_version(),gpu=gpu_info),
        bug_register=ISSUES)
    # Source-free export: strip source names (inventory) and all private profile/doc IDs.
    public=dict(summary);public['inventory']={k:v for k,v in summary['inventory'].items() if k!='sources'}
    save(root/'aggregate-metrics.json',public);save(root/'issues.json',ISSUES)
    matrix=np.load(root/'bge-documents.npy');projection=PCA(n_components=2,svd_solver='randomized',random_state=20261023).fit_transform(matrix)
    center=projection.mean(0);scale=np.max(np.abs(projection-center),axis=0);points=((projection-center)/np.maximum(scale,1e-8)).round(6).tolist()
    save_rows(root/'projection.jsonl',[dict(document_id=d['reference']['document_id'],x=p[0],y=p[1],method='PCA-not-relevance') for d,p in zip(docs,points)])
    # Compact all original bodies once. Rank traces and plans reference them by ID.
    selected=[]
    for row in batches:
        b=row['batch'];selected.append(dict(company_id=b['company_id'],variant=row['variant'],selected_context=b['selected_context'],budget=b['budget'],candidates=b['candidates']))
    plans=[]
    for row in rows(root/'analysis-plans.jsonl'):
        plans.append(dict(company_id=row['company_id'],variant=row['variant'],common=[dict(key=c['key'],document_ids=[s['reference']['document_id'] for s in c['data']['sources']]) for c in row['prepared']['common']],personal_key=row['prepared']['personal']['key']))
    data=dict(summary=summary,documents=docs,profiles=profiles,queries=queries,rankings=rankings,selections=selected,plans=plans,points=points,
        judgments=rows(root/'judgments-dev.jsonl')+rows(root/'judgments-test.jsonl'),groups=rows(root/'groups.jsonl'),
        exclusions=rows(root/'excluded.jsonl')+read(root/'e5-index.json')['exclusions'])
    template=Path(__file__).with_name('report.html').read_text()
    payload=json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html=template.replace('__REPORT_DATA__',payload);(root/'index.html').write_text(html)
    publicdir=Path('docs/reports/company-news-gpu-20261023');publicdir.mkdir(parents=True,exist_ok=True)
    save(publicdir/'metrics.json',public)
    paragraphs=['# Company → raw news: GPU experiment, 23 October 2026','',
        'Рабочая ветка `news-pipeline-gpu-lazy-retrieval` от `6648a15`. Общий контракт: `786a6e0`. Источники и локальные профили в Git не входят.',
        '', '3012 коротких публикаций из 3621; 609 длинных сохранены как исключения. 20 компаний для диагностики, 128 отдельно для скорости. BGE: 3012 документов / 2955 уникальных исходных представлений; E5: 2994, еще 18 превышают 512 токенов.',
        '', 'BGE использует закрепленную ревизию `5617a9f61b028005a4858fdac845db406aefb181`, FP32/CLS/L2, точный текст. Проверка повторным кодированием восьми источников: максимум абсолютной ошибки 1.49e-8. Запросы компаний кодируются в том же пространстве. E5 baseline — закрепленный CPU ONNX INT8, не смешан с BGE.',
        '', 'Cross-encoder: [BAAI/bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3), Apache-2.0, ревизия `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`, FP32. Лимит пары 2048 токенов, без обрезания. Русский smoke прошел; это проверка исполнения, не доказательство качества.',
        '', '2260 новых пар и 689 обращений к кэшу; inference 59.70 с. Пик выделенной VRAM 2.56 GB, зарезервированной 3.07 GB. RAM процесса около 3.70 GB. BGE импорт текстов 1.98 с, запросы 2.40 с; E5 документы 232.56 с и запросы 2.32 с. Эти времена различают импорт и новый inference. Точный cosine работает на CPU по GPU-векторам: BGE p50 4.87 мс / p95 6.03 мс на компанию (128 компаний); это только dense search, без BM25, загрузки и reranking.',
        '', 'Ручная проверка агента: 248 пар / 20 профилей, 115 dev (10 send, 100 reject, 5 uncertain), 133 test (1 send, 128 reject, 4 uncertain). Это не независимый человеческий gold и не общая CPU-разметка. Неразмеченное не считается отрицательным. Из-за единственного positive в test нельзя заявить превосходство модели.',
        '', 'По dev выбран G2 и резерв 4 исходных hybrid-кандидатов. Без резерва final known-positive recall 0.208, с резервом 4 — 0.458. Параметры заморожены до test. Recall относится к известным positives в пуле; final byte budget 24000 при max20 документов. Recall@20 отдельно сравнивает одинаковое число документов; реальное число при byte budget различается.',
        '', 'G4 не доказал экономию: 133 уникальные общие карточки против 122 у G3 на тех же 20 компаниях. Это подготовленные точные cache keys/запросы, а не оплаченные LLM-результаты. Новых live вызовов: 0. Для запуска требуются существующие общие журналы, max-calls, подтвержденный профиль и бюджет. Карточки и персональная интерпретация имеют разные ключи; ошибки не скрывают оригиналы.',
        '', 'Главные проблемы: пустые страницы и навигация ЦБ, географические совпадения без деловой связи, смешение банка и клиента, недостаток разметки. Сначала проверить версионированный content-kind фильтр и role-aware query/fusion, затем углубить общий размеченный пул. Reranker не исправляет пропуски retrieval. Историческая доступность профилей неизвестна; graph challenger не запускался. Общий контроль 344 не найден (локальный старый контроль — 100).',
        '', 'Полные оригиналы, исключения, все ранги, query paths, группы, выбранный контекст, ручные решения и 2D PCA доступны только локально: `data/processed/company_news/run-20261023/index.html`. PCA не является основанием релевантности. Офлайн-проверки и точные команды: `llm/event_pipeline/company_news/README.md`. Машиночитаемые агрегаты: `metrics.json`.']
    (publicdir/'report.md').write_text('\n'.join(paragraphs)+'\n')
    complete(root,'report',[root/'index.html',root/'aggregate-metrics.json',root/'projection.jsonl',root/'issues.json'],dict(source_bearing=True,html_bytes=len(html.encode()),public_export=str(publicdir)))
    return dict(local_html=str(root/'index.html'),public_report=str(publicdir/'report.md'))
