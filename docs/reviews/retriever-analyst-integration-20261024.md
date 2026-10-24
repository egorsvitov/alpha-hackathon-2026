# Retriever → Analyst: реализованный контракт, 24 октября 2026

Рабочая ветка `feature/analyst-retrieval-adapter` основана на обновлённом
`origin/feature/analyst` (`ef84f53`). Ретривер изучен по `origin/retriever`
(`04bde29`): `models.py`, опубликованный JSON Schema, `selection.py`, `store.py`,
merge adapter, модели/core merge и граничные тесты. Дополнительно проверены
реальные локальные пакеты. Рабочие файлы основного checkout не изменялись;
checkout retriever, указанный Git в `/tmp/retriever-merge-worktree`, отсутствовал.
Указанные в задании integration review и `cohort20-k20-20261024.md` отсутствовали
в доступном checkout и деревьях изучаемых веток. Этот документ — новый отчёт,
а не пересказ отсутствующего review. Локального cohort20-пакета не найдено.

## Соответствие трёх контрактов

| Назначение | Старый `impact_candidate_batch.v1` | Новый `retrieval-candidate-batch.v1` | `AnalystInput.v1-draft` / принятое решение |
|---|---|---|---|
| Запрос | `request.{request_id,company_id,role,as_of}` | Верхнеуровневые `request_id,company_id,as_of`; роли нет | `request`; роль обязательна отдельным аргументом нового пути |
| Окно / бюджет / лимит | `window_start/end`, `max_input_tokens`, `requested_limit` | `requested_k`; окна/бюджета аналитика нет | Явная `RetrievalAnalysisPolicy`: новое окно по умолчанию выключено (`0`); `max_input_tokens=8000`, `max_projection_chars=24000`, `max_insights=20` |
| Состояние | `ready/empty/degraded`, `empty_reason` | `ready/partial/empty`, `stop_reason`, `deferred` | `AnalystInputAndDiagnostics`; также `AnalystOutput.run_metadata.input_diagnostics`. Техническая недоступность не становится `not_relevant` |
| Профиль | Встроенные `company_context.verified_facts`, snapshot, profile hash/version | Только `company_id`, `profile_version` | Профиль передаётся отдельно. `features` с `quality=observed`, значением и origin → `company_context.verified_facts`; производные/неизвестные признаки → unknowns |
| Проверка версии профиля | Версия встроенного контекста | Отсылка к внешнему профилю | Company/version должны совпадать. Для нормализованного профиля `schema_version=0.1` повторно вычисляется content version по алгоритму `ranking.profiles.refresh_profile_version`; для минимальной проекции версия — непрозрачный тег |
| Интересы | `match_context` (каналы, признаки); hypotheses в схеме пусты | `interest_set_version`, `best_interest_id`, `interest_scores` | Внешний `CompanyInterestSet`; проверяются company/profile/set/encoder и активность ссылок. Текст и условия → `match_context.impact_hypotheses/assumptions_not_verified`, отдельно от фактов F |
| Сходство | Внешний сигнал отбора | `selection_score`, `embedding_score`, `score_origin` | Конечность и диапазон проверяются; числовые баллы не передаются LLM, `prior_score=None`. Нет пересчёта cosine в business materiality |
| Embeddings | Векторов во входе аналитика нет | `embedding_text`; в переданных интересах есть `vector` | Векторы и embedding text не попадают в prompt. Поля интересов выбираются по allowlist |
| Группа публикаций | `event_cluster_id` часто null; ранее ключ дедупликации | `group_ids`, а для event — `derived_from.cluster_id` | `Candidate.reading_group_ids`; **не ключ события**. `cluster_id` совместимого слоя получает ID события, поэтому существующая дедупликация аналитика сохраняет оба результата split |
| Одно событие | `members[0].bundle.event.event_id` | `item.item_id` для event, `sources[].article_id` для raw | `canonical_event_id=event:<item_id>` либо `article:<article_id>`. Raw identity — только идентичность статьи, не доказанное межстатейное событие |
| Кандидат | Готовый `candidate_id` | Готового ID кандидата нет | SHA-256 канонического JSON `[adapter_version, company_id, canonical_event_id]`; не зависит от роли, запроса или группы |
| Версия события | `snapshot.event_version` (int), `payload_sha256` | `item_version` (строка; у event — SHA-256 объекта event) | Полная строка в `projection.event.text_version` и `snapshot_ref`; `text_sha256` отдельно. Совместимое целое `event_version=1` не выдаётся за версию merge |
| Snapshot | B6 bundle + snapshot | SearchItem + исходные ArticleRevision | `event_bundle_refs.snapshot_sha256` — вычисленный хэш **проекции**, не подмена upstream text hash |
| Raw статья | Уже упакованные B6 claims/evidence | `item.kind=article`, `sources` | Проверяется совпадение item с исходной ревизией. Точные непрерывные фрагменты текста до 1200 Unicode code points → claims + evidence; берётся весь текст или явно удерживается весь кандидат |
| Merge | Уже упакованный EventBundle | `derived_from.event.{fragments,paragraphs,disagreements,...}` | Только оригинальные fragments → claims/evidence. Пересказ → `derived_context` с `origin=derived_merge`, `independent_evidence=false`, `semantic_entailment_checked=false` |
| Цитаты / координаты | B6 `evidence_ids`, `source_id`, `start/end` | Fragment `article_id` фактически ссылается на **revision_id**, `field,start,end,text,id` | Проверяются существование ревизии, поле, границы, точный срез, digest фрагмента, все paragraph/title/disagreement refs. `source_id=revision_id`; координаты `[start,end)` в Unicode code points |
| Противоречия | `conflicts[].variants` | `event.disagreements[].fragment_ids` | Производное описание конфликта + ссылки на оригинальные claims; QA сохраняет варианты/неопределённость. Семантическая истинность конфликта не доказана |
| Дата / URL | `sources[].published_at/url`; дата события B6 | У каждого ArticleRevision `published_at`, `publication_precision`, `url`, `observed_at` | Источники сохраняются с полным timestamp/offset, `day` или `unknown`, URL/null и text hash. Дата merge не используется. Aggregate first publication есть только когда все источники имеют timestamp |
| Повторы | ID кандидата/кластер | Повтор источника или item возможен в внешнем JSON | Одинаковая ревизия в sources схлопывается с заметкой; конфликтующие ревизии — ошибка пакета. Конфликтующие представления одного canonical event удерживаются все, без выбора первого. Повтор ссылки внутри item — ошибка кандидата. Одно событие в нескольких reading groups схлопывается с объединением group IDs; два разных события с общей исходной публикацией сохраняются |
| Кэш | Company/event/version/profile/role/prompt/model; offline loose key | Больше внешних факторов контекста | Для нового входа к обоим ключам добавляется `retrieval_context_hash`: проекция, профиль, переданные интересы/версия, дата запроса, роль и policy. Старые ключи сохранены |

## Валидация и пределы проверки

Новый модуль: `backend/app/analyst/retrieval_adapter.py`.
Опубликованный JSON Schema из `04bde29` сохранён без изменения в
`backend/app/analyst/contracts/RetrievalCandidateBatch.v1.schema.json`.
Используется `Draft202012Validator`; даты/таймзоны, хэши, состав исходников и
ссылки дополнительно проверяются кодом. Ошибки схемы уровня кандидата изолированы;
ошибка идентичности контекста, источников или пакета вызывает `BatchValidationError`.
Диагностические ошибки не включают исходный текст и значения приватных признаков.

В локальных merge-v2 JSON обнаружены два поля, которых **нет** в опубликованной
схеме origin: `processing_status`, `merge_fallback`. Адаптер явно расширяет
проверяемые свойства Candidate только этими полями. Известные состояния:
`ready`, `merge_fallback`, `partial`, `deferred`, `unresolved`, `failed`.
Неизвестные поля не игнорируются. В старом локальном пакете без этих расширений
связь raw `group_ids` с `deferred.cluster_id` явно помечает raw fallback.

Обязателен предоставленный `text_sha256`: адаптер не заполняет отсутствующий
хэш оригинала. Хэш версии event пересчитывается; digest каждого fragment
проверяется по `[revision_id,field,start,end,text]`. Ссылки на merge
operation/result/config имеют формат SHA-256, но полные MergeResult/config
не переданы — пересчитать их digest невозможно. Это ограничение, не выполненная
криптографическая проверка receipt. Источник или наблюдение после `as_of`
отклоняют кандидата; `day` сравнивается как календарная дата, без выдуманного
времени/таймзоны. Историческая доступность при неизвестном observed_at не доказана.

Проверяется структура HTTP(S) URL. Сетевой запрос к источнику не выполняется:
существование внешней страницы и полнота собранного оригинала не гарантируются.
«Ссылка существует» в доказательствах означает локальную ссылочную целостность,
а не доказательство смыслового следования пересказа или истинности новости.
Сгенерированный title не принимается как оригинальная цитата. Числа из
производного заголовка не расширяют набор допустимых чисел в QA.

Проекция ограничивается целиком: при превышении лимита символов проекции либо
оценки полного промпта кандидат получает `context_too_large`, список omitted source
и claim IDs и не передаётся модели. Никакие цитаты, unknowns нового входа или
merge paragraphs тихо не обрезаются. Оценка токенов использует существующий
`prompt_budget` (UTF-8 bytes / 4); это оценка, не точный tokenizer провайдера.
Повторная проверка непосредственно перед анализом включает реальный блок памяти
и few-shot. Если он переполняет лимит, результат имеет явный reason
`context_too_large`, а диагностика обновляется. Уменьшение проекции в этой версии
не реализовано: выбирается разрешённый контрактом вариант явного отказа по бюджету.

## Вызов и публичный пример

```python
from app.analyst.retrieval_adapter import adapt_retrieval_batch

adapted = adapt_retrieval_batch(
    batch, company_profile, active_interests, "cfo",
    {"max_input_tokens": 8000, "max_projection_chars": 24000},
)

output, adapted = service.analyze_batch(
    batch, "cfo", company_profile=company_profile,
    active_interests=active_interests,
    analysis_policy={"max_input_tokens": 8000},
)
```

`company_profile` — нормализованный профиль с company/profile version и features
`{feature_id: {feature_id,path,value,quality,origin:{kind,source_path}}}`.
Минимальная проекция может не объявлять `schema_version`; объявленная неизвестная
версия отклоняется. Контекст следует получать из доверенного backend profile
store: совпадение ID/хэша не заменяет аутентификацию владельца компании.
`active_interests` — версия набора и список интересов с явным `active`.
Роль нового входа обязательна. Старый `analyze_batch(old_batch, role=None)`
сохраняет прежний путь `ImpactBatchAdapter`.

В `docs/examples/retriever-analyst/` опубликованы только синтетические данные:
`batch.json`, `company-profile.json`, `active-interests.json`, преобразованный
`analyst-input.json` и `diagnostics.json`. Пример содержит raw article и два
разных события одной reading group после split, включая событие из двух изданий.
Воспроизводимость выходных JSON проверяется тестом.

## Проверки

Проверки выполняются на FakeLLM/локальных данных, без платных вызовов.

```bash
# Из backend; .venv основного репозитория содержит зависимости.
python -m unittest discover -s tests/analyst -t . -q

# Дополнительно: private local corpus, только чтение, без записи исходных JSON.
ANALYST_RETRIEVAL_LOCAL_DIR=/path/to/data/processed/retriever \
  python -m unittest tests.analyst.test_retrieval_adapter -q

# Из корня, дополнительные регрессии.
python -m unittest discover -s ranking/tests -q
ANALYST_MODE=template python backend/test_e2e.py
ANALYST_MODE=cached python backend/test_e2e.py
```

Финальный прогон (Python 3.14.7, Linux): **469 тестов аналитика — OK,
4 пропуска**; новый набор **35/35 — OK** с включёнными локальными данными;
**53/53 ranking — OK**; оба backend E2E (`template`, `cached`) — OK,
`llm_calls=0`; компиляция Python и `git diff --check` — OK. Четыре обычных
пропуска — три прежних теста с необязательными внешними handoff-файлами и один
opt-in тест приватного локального corpus. С включённым локальным corpus новый
набор проходит без пропусков.

Локальная совместимость проверена на **42 batch.json, 300 кандидатах**
(266 raw, 34 event), в том числе исторических вариантах. При policy по умолчанию
294 кандидата допущены в аналитик, 6 удержаны с `context_too_large`; ошибок схемы,
цитат или связей не найдено. Это проверка контракта, не оценка качества LLM.
Пакеты содержат повторные компании и историю запусков; число 42 не означает
42 независимые компании. Исходные новости, профили, vectors, БД и trace/API logs
не включены в ветку.

## Открытые вопросы контракта

1. Опубликовать согласованную схему с processing_status/merge_fallback и описать
   payload состояний. Сейчас adapter поддерживает наблюдавшееся расширение явно,
   но origin/retriever всё ещё объявляет Candidate additionalProperties=false.
2. Дать отдельный устойчивый canonical_event_id и политику его сохранения при
   повторном merge/repartition. Текущий merge event ID зависит от operation/draft;
   adapter гарантирует дедупликацию одинакового event ID, а не тождество разных
   пересборок или двух raw статей об одном событии.
3. Определить хэш именно активного набора интересов. В изученном эксперименте
   interest_set_version — хэш каталога; adapter сравнивает версию и включает
   фактически переданные тексты/условия в cache fingerprint, но не может доказать
   комплектность выбора относительно отсутствующего полного каталога.
4. Передавать полный versioned merge receipt/config, если нужна проверка result
   digest и полноты coverage. Существующие фрагменты и refs не доказывают
   семантическую корректность/полноту объединения; для неё нужен отдельный QA.
5. Согласовать бизнес-окно, политику неизвестной даты/таймзоны day и tokenizer
   бюджета. В новом batch эти параметры аналитика не заданы; адаптер не берёт их
   из косвенных времён и не заменяет дату публикации датой merge.
6. Определить долговременное хранилище исходных ревизий/координат и правила
   позднего исправления/удаления публикации. Сейчас проекция самодостаточна для
   проверки переданных фрагментов при наличии batch, но полные originals
   намеренно не сохраняются в репозитории.

## Что требуется для ленты кабинета

Текущая задача подключает **вход сервиса аналитика**. `store.get_feed()` по-прежнему
использует прежние events/score_fn; worker retriever сам в кабинет не вызовется.
Следующий backend этап:

1. Job/оркестратор берёт согласованные batch + актуальные profile/interest snapshots,
   передаёт роль и policy в `analyze_batch`, сохраняет AnalystOutput и диагностику
   в рабочее хранилище с идемпотентностью по company/candidate/context fingerprint.
2. Связать `candidate_id`/canonical event с карточкой события и исходными
   source revision IDs. Для нового пути нужен mapper AnalystOutput → FeedItem/
   Insight, который использует проверенные цитаты и source URL из adapted input;
   нельзя `zip`-ом сопоставлять старый список events после отклонений/дедупликации.
3. Расширить feed/event API метаданными reading groups, полной строковой версией
   текста и его хэшем, точностью даты и null для неизвестной даты. Старый NewsEvent
   требует строковую date и не умеет честно показать неизвестную дату.
4. Передавать partial/deferred/unresolved/invalid/context_too_large в состояние
   обработки/повтора, не скрывать их как неинтересные новости. Отдельно отображать
   действительно `decision=exclude` после бизнес-анализа.
5. Подключить банк RAG и реальные graph exposures отдельно при наличии их
   происхождения. Поисковые интересы не подтверждают поставщиков/покупателей;
   пустой bank_fragments означает отсутствие переданной позиции банка.
6. Добавить API/UI регрессию на split, неизвестные даты, дубликаты и технические
   состояния, затем проверить качество анализа на согласованной выборке в рамках
   отдельного разрешённого бюджета. В текущей работе live-LLM не вызывалась.

## Зависимости и воспроизводимость окружения

`jsonschema` уже объявлен в `backend/requirements.txt`, но отсутствовал в
Docker `requirements.lock`. Lock дополнен jsonschema/attrs/specifications/
referencing/rpds-py через `uv pip compile --python-version 3.13` с исходным lock
как constraint: старые версии остальных пакетов сохранены. Docker-образ
в этой работе не собирался. Локальные зависимости установлены в `.venv`
основного репозитория; кэш uv также внутри `.venv`. Для расширенных ranking
тестов установлены оба валидатора форматов, уже перечисленные в pyproject.toml.
Проверки API/E2E выполнялись вне файловой/сокетной песочницы: TestClient и
network-guard требуют локальных сокетов. Внешние LLM-запросы не выполнялись.
