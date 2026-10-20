# Контракт API для фронтенда

Документ для того, кто делает backend. Здесь написано, какие данные нужны фронту и в каком виде их отдавать, чтобы вместо моков в интерфейсе появились настоящие данные.

Пути взяты из заглушек в `backend/app/api/v1/endpoints.py`, префикс `/api/v1` уже есть в `backend/main.py`. Меняется в основном содержимое ответов. Формы ответов описаны типами в `frontend/src/types.ts`. Если что-то расходится, верьте `types.ts`, а этот документ поправьте.

Готовые примеры ответов лежат в `docs/api-examples/`. Их можно положить в заглушки как есть: фронт на них проверен (см. раздел «Проверка»).

## Как фронт подключается

В `frontend/.env`:

```
VITE_API_MODE=http
VITE_API_URL=http://localhost:8000
```

Без этого фронт работает на моках в браузере. CORS в `main.py` уже открыт. Все запросы идут через `frontend/src/api/index.ts`, других обращений к сети нет.

## Общие правила

- JSON в UTF-8. Названия полей как в таблицах ниже, `snake_case`.
- Даты. Фронт берёт первые 10 символов (`YYYY-MM-DD`), так что можно отдавать и просто дату, и ISO-время с поясом (`2026-09-11T15:00:00+03:00`).
- Роли пользователя: `ceo`, `cfo`, `treasurer`, `chief_accountant`. Фронт передаёт выбранную роль в каждом запросе. Роль влияет на порядок событий в ленте и на порядок шагов в карточке: порядок задаёт backend, фронт ничего не пересортировывает.
- Неизвестное значение отдавайте как `null`, а не `false` или `0`. Для интерфейса «нет продукта» и «про продукт ничего не знаем» разные вещи: второе показывается как «неизвестно».
- Поля с пометкой «необязательно» можно не отдавать совсем, фронт это переживёт.
- Любой ответ не 2xx фронт показывает как «Не удалось загрузить» с кнопкой «Повторить». Тело ошибки не читается. Для неизвестной компании или события отдавайте 404.
- Права доступа (пользователь видит только свои компании) проверяет backend. Фронт просто подставляет `company_id` из выбора.
- Всё синтетическое помечайте `is_synthetic: true`, фронт покажет пометку «демо-данные». Для настоящих материалов банка и СМИ ставьте `false`.

## Эндпоинты

| Метод и путь | Запрос | Ответ |
|---|---|---|
| `GET /api/v1/companies` | — | список `Company` |
| `GET /api/v1/companies/{company_id}/news-feed?role=` | `role` | список `FeedItem`, уже отсортированный: самое значимое первым |
| `GET /api/v1/news/{news_id}/analysis?company_id=&role=` | `company_id`, `role` | один `Insight` |
| `POST /api/v1/chat/ask` | `{company_id, news_id, role, run_id, question}` | `QaAnswer` |
| `POST /api/v1/news` | `{title, text}` | `NewsEvent` |
| `POST /api/v1/feedback` | `{run_id, useful}` | любой 2xx, можно 204 без тела |

`news_id` в путях это `event.event_id` из ленты.

`POST /news` нужен для загрузки новой новости из интерфейса. Её надо прогнать тем же путём, что и остальные, вернуть созданное событие с `user_uploaded: true`. Сразу после ответа фронт заново запросит ленту и откроет `GET .../analysis` для нового события, так что оно должно в них появиться. Текст может быть пустым, заголовок всегда есть.

Примеры: `companies.json`, `feed.json`, `analysis.json`, `analysis-minimal.json`, `ask.json`, `news-upload.json`.

## Типы

### Company

Нужен для выбора компании и блока «Что мы знаем о вашей компании». В данных кейса у компаний нет названий, а интерфейсу нужна подпись. Придумайте её из доступного (например, «Компания U_COMP_001») и отдавайте в `name`.

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `company_id` | string | да | идентификатор, уходит во все запросы |
| `name` | string | да | название в шапке и списке выбора |
| `industry` | string | да | отрасль словами, не код ОКВЭД |
| `revenue_bn_rub` | number | да | выручка в млрд ₽ (из `REVENUE_COMPANY` в рублях делите на 1e9) |
| `region` | string | да | регион |
| `age_label` | string | да | возраст компании как в градации из данных, диапазон в число не превращайте |
| `products` | object | да | флаги `rko`, `guarantees`, `loans`, `leasing`, `deposits`, `payroll`, `factoring`; каждый `true`, `false` или `null` |
| `fts` | object | да | `{flag: true/false/null, country: string/null}` |
| `profile_version` | string | да | версия профиля, показывается под профилем |
| `is_synthetic` | true | да | пометка демо |
| `initials` | string | нет | две буквы для плитки; если нет, фронт возьмёт из названия |
| `okved_code`, `okved_name`, `stage`, `workers_label`, `city`, `group_id`, `counterparties`, `feature_keys` | | нет | пока в интерфейсе не показываются; можно отдавать для будущих экранов |

Соответствие полям датасета: `CL_PRODUCT_RKO_MARK` → `products.rko`, `..._GAR_` → `guarantees`, `..._LOA_` → `loans`, `..._LEA_` → `leasing`, `..._DEP_` → `deposits`, `..._ZPP_` → `payroll`, `FACTORING_MARK` → `products.factoring`, `FTS_FLAG` и `FTS_COUNTRY_NAME` → `fts`.

### NewsEvent

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `event_id` | string | да | идентификатор события |
| `title` | string | да | заголовок |
| `date` | string | да | дата публикации |
| `topic_label` | string | да | подпись темы в строке ленты («Ставки и кредит») |
| `sources_count` | integer | да | сколько публикаций объединено в событие |
| `is_synthetic` | boolean | да | пометка «демо-данные» |
| `topic` | string | нет | код темы, интерфейс по нему ничего не решает |
| `dedup_note` | string | нет | пояснение про объединённые дубли |
| `user_uploaded` | boolean | нет | событие загружено из интерфейса |
| `synthetic_link` | string | нет | если новость привязана к обезличенной компании искусственно, здесь текст-предупреждение; он выводится под заголовком |

### Relevance

Значимость события для выбранной компании и роли.

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `score` | number 0..1 | да | итоговый балл, показывается в окне «Почему такая значимость» |
| `level` | `high` / `medium` / `low` | да | знак значимости и цвет; других значений нет, `unknown` из схемы агента замените на `low` |
| `explanation` | string | да | короткая причина, например «Связано: ваши кредитные продукты, лизинг» |
| `factors` | массив | да, может быть пустым | вклад факторов: `{feature_key, label, contribution, detail}` |
| `reasons` | массив строк | нет | если числового вклада нет, в окне покажутся эти строки |

Чем больше `contribution`, тем длиннее полоска. Вклады в сумме должны давать `score`, так они показаны в окне. Метки вроде свежести события или числа источников можно передавать как обычные факторы.

### FeedItem

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `event` | `NewsEvent` | да | |
| `relevance` | `Relevance` | да | |
| `why_short` | string | да | одна строка «Вам: …»; приставку «Связано:» фронт отрежет сам |
| `has_bank_position` | boolean | да | пометка «есть позиция банка» в ленте |
| `profile_version` | string | да | |

Порядок в массиве — порядок на экране. Подсчёт «важных» для виджета и вкладок фронт делает сам по `relevance.level`.

### Insight

Карточка события для компании и роли. Если бэку нечего положить в блок, отдавайте пустой массив или `null`, интерфейс подставит честную подпись («Существенных рисков не выявлено», «Позиции банка по этому событию нет» и т. п.).

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `run_id` | string | да | идентификатор запуска агента; уходит в `/chat/ask` и `/feedback` |
| `company_id`, `profile_version`, `user_role` | string | да | |
| `status` | `ok` / `insufficient_data` | да | при `insufficient_data` вместо блоков показывается «Недостаточно данных для вывода» |
| `generated_by` | `live` / `cached` | да | `cached` включает пометку «показан сохранённый результат» |
| `event` | `NewsEvent` | да | |
| `relevance` | `Relevance` | да | |
| `what_happened` | `{summary, facts: Claim[]}` | да | раздел «Что случилось» показывает `facts`; `summary` (одно предложение) выводится только при `insufficient_data` |
| `why_it_matters` | `Claim[]` | да | «Почему важно вам» |
| `risks` | `Claim[]` | да | «Риски» |
| `opportunities` | `Claim[]` | да | «Возможности» |
| `next_actions` | `Claim[]` | да | «Следующий шаг», нумеруется в порядке массива; шаги для выбранной роли кладите первыми |
| `bank_position` | `{as_of, claims: Claim[]}` или `null` | да | «Позиция банка», только аналитика банка; `as_of` — дата материала |
| `ai_conclusion` | `{text, evidence_ids, features}` или `null` | да | «Вывод AI», краткий вывод модели; при `null` блока нет |
| `unknowns` | `Unknown[]` | да | «Что неизвестно» |
| `evidence` | `Evidence[]` | да | источники и признаки клиента |
| `trace` | `TraceStep[]` | да, может быть пустым | «Как получен вывод» |

Позиция банка и вывод AI это разные блоки по смыслу и по оформлению: в `bank_position` кладите только то, что действительно сказано в материалах банка, всё остальное идёт в `ai_conclusion`, `risks` и т. п. Если подходящей аналитики банка нет, отдавайте `null`. Придумывать позицию банка нельзя.

#### Claim

Одно утверждение с привязкой к основаниям.

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `text` | string | да | текст |
| `evidence_ids` | string[] | да, может быть пустым | на какие источники опирается; рядом с текстом появятся кружки с номерами |
| `features` | string[] | нет | какие признаки клиента повлияли; выводятся строкой «Основано на: …» |
| `conditional` | boolean | нет | утверждение условное, перед ним ставится «Условно:» |
| `roles` | Role[] | нет | для каких ролей шаг; шаги для выбранной роли идут жирнее и помечены «Для вас» |

Каждый вывод ИИ должен иметь хотя бы один признак в `features` или источник в `evidence_ids`, иначе непонятно, на чём он основан.

#### Evidence

Элемент массива `evidence`. Сюда кладутся и источники, и признаки клиента.

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `evidence_id` | string | да | на него ссылаются `evidence_ids` |
| `type` | `news` / `bank_research` / `synthetic` / `company_feature` | да | подпись типа и значок |
| `title` | string | да | заголовок материала или название признака |
| `publisher` | string | да | издатель; для аналитики банка «Центр макроэкономических исследований Альфа-Банка» |
| `date` | string или null | да | дата публикации |
| `quote` | string | да | цитата, на которую опирается вывод |
| `is_synthetic` | boolean | да | для материалов банка `true` добавляет «демо-цитата»; для настоящих отдавайте `false` |
| `doc` | string | нет | документ или поле данных; для аналитики банка файл, для признака поле датасета |
| `page` | integer | нет | страница документа |
| `url` | string | нет | сейчас не показывается |

Нумерация источников на экране идёт по порядку элементов в `evidence`, кроме `company_feature`. Кладите источники в том порядке, в каком они впервые встречаются в тексте.

Признак клиента кладётся в `evidence` с `type: "company_feature"` и `evidence_id: "cf:<feature_id>"`, где `<feature_id>` это то, что лежит в `features` у утверждений. В `title` пишите человеческое название («Кредитные продукты»), в `quote` значение вместе с полем: `CL_PRODUCT_LOA_MARK = 1 (есть)`. Название из `title` фронт подставит в строку «Основано на».

#### Unknown

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `text` | string | да | чего не хватает |
| `how_to_clarify` | string | нет | как это выяснить |
| `question` | string | нет | готовый вопрос; рядом появится кнопка «Спросить об этом», она отправит его в `/chat/ask` |

Условия договора, объём долга, тип ставки и подобное в профиле неизвестны. Выводы, которые от них зависят, должны быть условными (`conditional: true`), а сами пробелы перечислены здесь.

#### TraceStep

Что делал агент. Нужно для критерия про трассируемость: видно, какие инструменты вызывались и был ли повторный поиск.

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `n` | integer | да | порядок |
| `tool` | string | да | имя инструмента, например `search_bank_research` |
| `summary` | string | да | что запрашивалось |
| `result` | string | да | что получилось |
| `status` | `ok` / `retry` / `warn` | да | `retry` — шаг, который пришлось повторить; `warn` — результат с оговоркой |

Под заголовком раздела фронт пишет число шагов со `status: "retry"` как «повторных поисков».

### QaAnswer

Ответ на уточняющий вопрос по карточке.

| Поле | Тип | Обязательно | Как используется |
|---|---|---|---|
| `text` | string | да | ответ |
| `evidence` | `Evidence[]` | да, может быть пустым | источники ответа, выводятся под текстом |
| `did_extra_search` | boolean | да | агент делал дополнительный поиск |
| `extra_search_note` | string | нет | что искали; показывается над ответом |
| `from_cache` | boolean | да | `true` добавляет пометку «Сохранённый ответ» |
| `insufficient` | boolean | да | данных не хватило, ниже появится «Данных для уверенного вывода недостаточно» |
| `evidence_ids` | string[] | нет | в интерфейсе не используется |

Если ответа в источниках нет, агент должен так и сказать (`insufficient: true`), а не придумывать.

## Как собрать это из того, что уже есть в репозитории

Схема агента `llm/agent/contracts/insight.schema.json` (v0.1) и ответ фронта устроены похоже, но не совпадают. Таблица показывает, откуда что брать.

| Поле фронта | Откуда |
|---|---|
| `run_id`, `company_id`, `user_role` | те же поля схемы агента |
| `profile_version` | `features_used[].origin.profile_version`; если там нет, версия из слоя профилей |
| `status` | `ready`, `limited` → `ok`; `needs_clarification`, `insufficient_evidence` → `insufficient_data`; `budget_exhausted` → `ok` с `generated_by: "cached"`, если есть сохранённый результат, иначе `insufficient_data`; `failed` → HTTP 5xx |
| `relevance.score`, `relevance.level` | `significance.score`, `significance.level` (`unknown` → `low`) |
| `relevance.explanation`, `relevance.reasons` | `significance.reasons` (первая причина как объяснение, весь список как `reasons`) |
| `relevance.factors` | числовой вклад факторов должен отдавать модуль ранжирования (`ranking/`); в схеме агента его пока нет |
| `what_happened.facts` | `claims` с `section = what_happened` |
| `why_it_matters` | `claims` с `section = why_important` |
| `risks`, `opportunities`, `next_actions` | `claims` с `section` = `risk`, `opportunity`, `next_step` |
| `bank_position.claims` | `claims` с `section = bank_view`; `as_of` — самая поздняя `published_at` среди их источников |
| `Claim.evidence_ids`, `Claim.features` | `source_ids`, `feature_ids` |
| `Claim.conditional` | `support_status` равен `conditional` или `pending_review`, либо `assumptions` не пуст |
| `Claim.roles` | в схеме агента нет; нужно добавить, если шаги должны зависеть от роли |
| `ai_conclusion` | в схеме агента нет; нужно либо поле с кратким выводом, либо `null` |
| `unknowns[].text` | `limitations` и `claims[].assumptions` |
| `unknowns[].how_to_clarify`, `question` | в схеме агента нет, необязательны |
| `evidence` из источников | `sources`: `news` и `synthetic_news` → `news` или `synthetic`, `bank_analytics` → `bank_research`; `locator` → `doc`, `quote`, `page`, `published_at` → `date` |
| `evidence.title`, `evidence.publisher` | в схеме источника этих полей нет; название документа брать из `data/processed/document_catalog.json` |
| `evidence.is_synthetic` | `verification_status = synthetic` или `type = synthetic_news` |
| `evidence` из признаков | `features_used`: `cf:<feature_id>`, `type: company_feature`, `title` — читаемое название, `quote` — `path = value` |
| `trace` | шаги из журнала вызовов агента; в схеме сейчас только `artifact_ids`, `qa_status`, `retry_count`, поэтому шаги надо либо добавить, либо отдавать `[]` |
| `changes_since_previous`, `usage` | фронт не показывает, на бэке можно оставить |

Что менять в заглушках `endpoints.py`:

- `CompanyBase` (`synthetic_u`, `mainokved_name`, `revenue_company`, `city`) заменить на `Company` выше; `synthetic_u` станет `company_id`, выручку перевести в млрд.
- `NewsItemBase` заменить на `FeedItem`.
- `AnalysisCard` заменить на `Insight`; в запрос `analysis` добавить `role`.
- `ChatRequest` дополнить `role` и `run_id`; в `ChatResponse` вместо `answer` и `sources` отдавать `text` и `evidence`.
- Добавить `POST /news` и `POST /feedback`.

## С чего начать

Чтобы фронт заработал на живых данных, хватит такого порядка.

1. `GET /companies`: только обязательные поля.
2. Лента: `event`, `relevance` (с пустым `factors`), `why_short`, `has_bank_position`.
3. Карточка: `what_happened`, `why_it_matters`, `evidence`, остальные списки пустые, `bank_position` и `ai_conclusion` равны `null`. Пример такого ответа: `analysis-minimal.json`.
4. Потом риски, возможности, шаги, позиция банка, вывод AI, неизвестное.
5. Потом чат, загрузка новости, отзыв и шаги трассы.

## Проверка

Фронт в режиме `http` прогнан на заглушке, которая отдаёт файлы из `docs/api-examples/` по путям из таблицы выше. Прошли все шесть запросов (включая `204` на `/feedback`), ошибок в консоли браузера нет. Вторая компания в `companies.json` специально сделана с минимумом полей, а в `feed.json` у второго события дата с временем и пустой `factors`, чтобы проверить, что интерфейс это переносит.

Проверить свой бэк так же:

```bash
cd backend && uvicorn main:app --reload --port 8000
curl "http://localhost:8000/api/v1/companies"
curl "http://localhost:8000/api/v1/companies/SYNTHETIC_U_1042/news-feed?role=cfo"
curl "http://localhost:8000/api/v1/news/e5_coke_supplier/analysis?company_id=SYNTHETIC_U_1042&role=cfo"
cd ../frontend && cp .env.example .env   # поставить VITE_API_MODE=http
npm install && npm run dev
```
