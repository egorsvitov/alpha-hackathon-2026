# Приёмка полного backend цикла — 24.10.2026

Ветка `integration/full-cycle-backend`, worktree `/tmp/full-cycle-backend`.
Upstream refs обновлены перед началом: main `04500c1`, retriever `04bde29`,
analyst adapter `6cf4bc1`. Локальные изменения исходного worktree не изменялись.
Коммит результата: `git rev-parse HEAD` в интеграционной ветке.

## Результат

Collector JSONL → immutable revisions → проверенный BGE/native bundle → существующие
reading groups → SQLiteSearchStore → RetrieverService → lazy MergeService →
эмбеддинги всех событий и atomic apply → AnalystService.analyze_batch → сохранённые
jobs/cards → прежние feed/analysis HTTP endpoints → HTTP кабинет.

Запущенный проверочный экран: **http://localhost:18080**, backend Swagger:
**http://localhost:18000/docs**. Compose project `full-cycle-proof`, отдельный том
`full-cycle-proof_backend-data`. В нём **явно синтетические** материалы; transport
моделей подменён, API и хранилища настоящие. Рабочий каталог standalone проверки:
`/tmp/full-cycle-acceptance`; реальный офлайн-прогон:
`/tmp/full-cycle-real`; приватный native export:
`/tmp/full-cycle-real-input`. Оригиналы и LLM runtime данные в Git не добавлены.

Команды полного воспроизведения, curl и описание schema/storage:
[full-cycle-backend.md](../full-cycle-backend.md).

## Автономный путь

Свежий запуск synthetic worker в backend Docker image:

| Показатель | Результат |
|---|---:|
| Публикации / immutable revisions | 4 / 4 |
| Reading groups | 2 |
| Активные события после merge/split | 3 |
| Компании × роли | 2 × CEO/CFO |
| Сохранённые карточки | 12, по 3 на контекст |
| Fake merge transport calls | 3 |
| Fake analyst calls | 12 |
| Вычисленные embedding texts | 6: 3 уникальных исходных + 3 событийных |
| Платные вызовы | 0 |

Две публикации объединяются в одно событие. Две разные новости внутри другой
reading group остаются двумя карточками; общий group ID не удаляет одну из них.
Проверены исходные revision/candidate/canonical event IDs, HTTPS URL, Unicode
координаты цитат, неизвестная дата=null и явная маркировка derived_merge.

Повтор worker после рестарта двух контейнеров:
`changed=false`, `new_articles=0`, все четыре задания `reused=true`,
`new_fake_merge_calls=0`, `new_fake_analyst_calls=0`, `new_encoded=0`.
Ответ HTTP ленты до и после рестарта сравнен целиком: **совпадает**. Карточка
успешно прочитана через nginx frontend. `/config.js`: `apiMode: "http"`.

## Реальный локальный корпус

Использованы локальные артефакты `run-20261023` и activity interests BGE-M3.
Проверены ARTIFACTS.sha256, хэши всех файлов interest manifest, соответствие матрицы
и mapping, exact source hashes, нормализация и fingerprint. Новых векторов в реальном
прогоне не вычислялось; старые E5/другие пространства не подставлялись.

| Показатель | Результат |
|---|---|
| Статьи: новые / всего | 3012 / 3012 |
| Векторы корпуса | 3012, переиспользованы |
| Encoder fingerprint | `5b47885f9770f507a07300d371ca5ff0ba1ea4b93681db1b6ee950f8f18e46b9` |
| Закреплённая модель | BAAI/bge-m3, revision `5617a9f61b028005a4858fdac845db406aefb181` |
| Corpus source SHA-256 | `c8898512bcc4ba3cc3f9c81be61d39035538c94052cded2259631546fa848ce8` |
| Группы существующего снимка | 1430, из них 118 с неполным составом |
| Доверенные компании / активные интересы | U_100013: 5; U_100090: 3 |
| Выполненный поиск | U_100013, CFO, k=3, as_of `2026-10-24T00:00:00+03:00` |
| Политика | явный raw_fallback, платный merge выключен |
| Кандидаты | 3 |
| Состояния задания | analysis_failed: 3; deferred: 2; общий partial |
| Платные вызовы / токены / рубли | 0 / 0 / 0 |
| Время первого импорта | 5,12 s |
| Время retrieval + adapter + offline analyst | 2,06 s |
| Повтор после нового процесса | 1,62 s, changed=false, reused=true, 0 новых статей |
| Пик RAM повторного процесса | 279,9 MiB |
| VRAM | не использовалась |

Первый реальный job ID:
`ffe7c70274074cd4dc5d6bd8858b71129f9cb36848cbfa6d9d1fff0911a0588e`.
Profile version U_100013:
`91799b29ab6cb1d2e63c4d0e68d90434430c6e5495bc7f5a7fbf354be68f55cb`.
Interest set version U_100013:
`7940bf915946a6aea598cd6e0da2466a740f1a4f5cb78a7d4892a7f580fb98a1`.
Index input identity:
`6e9d75594e09c136f513807cd1299fb312db329b47d07ab67193fcda7b58bd38`.
Полные batch, trace, diagnostics, profile/interest snapshots, AnalystOutput и outcomes
находятся в приватном `pipeline.sqlite3`. Технический отказ не назван exclude;
готовые карточки аналитика не выдуманы. Видимая карточка подтверждена автономным тестом.

Отдельный benchmark полного пересчёта существующим complete-link на тех же 3012
BGE векторах: **1547 групп, 6,60 s, max RSS 487096 KiB (475,7 MiB)**.
Параметры threshold=0.75, alpha=0, scipy, neighbors=0; эти группы не подменяли
существующий исторический снимок с другими параметрами. Граница default grouping
задокументирована: до 5000 единиц / 512 MiB оценки матрицы.

**Live подтверждён после отдельного явного разрешения пользователя.** Выполнен
один анализ одной новости с двумя наблюдаемыми признаками ОКВЭД U_100013 через
`https://api.alfahack-gateway26.ru/v1`, модель `z-ai/glm-5.3-flash`, промпт
`analyst-v1.2`. HTTP 200, QA passed, decision=exclude. Фактическое списание
**0,08894539172091 ₽**, подтверждено изменением `info.spend`; повторы/merge/repair — 0.
Рекомендательная карточка не создаётся для exclude. HTTP повтор и новый процесс
аналитика подтвердили чтение сохранённого результата/дискового кэша без модели.
[Подробный обезличенный live-отчёт](llm-live-check-20261024.md).

## Регрессия и эксплуатация

Локально Python 3.14.7, numpy 2.5.4, pydantic 2.14.1;
Docker backend/collector Python 3.13, frontend node:24-alpine.

| Команда / проверка | Результат |
|---|---|
| `PYTHONPATH=.:backend python -m pytest retriever/tests llm/cluster_merge/tests backend/tests/test_full_cycle.py -q` | 134 passed + 7 subtests |
| `PYTHONPATH=.:backend python -m unittest discover -s backend/tests/analyst -t backend -q` | 469 tests, OK, 4 ожидаемых opt-in skips |
| `python -m unittest discover -s ranking/tests -q` | 53 tests, OK |
| `PYTHONPATH=.:backend ANALYST_MODE=template python backend/test_e2e.py` | OK, llm_calls=0 |
| `PYTHONPATH=.:backend ANALYST_MODE=cached python backend/test_e2e.py` | OK, llm_calls=0 |
| Frontend `npm ci` + `npm run build` внутри Docker | TypeScript + Vite OK |
| `docker compose config --quiet` | OK |
| Compose config с full-cycle worker override и явными путями | OK |
| `docker compose build backend frontend news-collector` | все 3 образа собраны |
| Synthetic worker в backend и CPU image (`--network none`), HTTP через nginx, restart, повтор worker | OK |
| `docker build -f backend/Dockerfile.encoder -t alfa-news-worker:local .` | CPU image собран; offline imports torch 2.14.1+cpu, sentence-transformers 5.7.0, transformers 5.20.2, scipy 1.18.1 — OK |
| `python -m compileall -q backend/app/full_cycle news`, `git diff --check` | OK |

24 новых теста полного цикла включают: file→HTTP, merge/split, отсутствие ложного
дедупа, рестарт, exact evidence/URL links, fingerprint mismatch, immutable revision
conflict, новый текст→новая редакция, повреждённый native bundle, отказ импорта,
LLM failure/budget/context, неизвестную дату, отсутствующий merge, raw fallback,
незавершённый merge, stale profile, пустой здоровый сбор и отказ всех источников.
Дополнительно проверено, что неизменённый корпус после рестарта не загружает модель
и не повторяет parity; настоящий fingerprint проверяется перед новыми vectors.
Тесты блокируют внешний HTTP transport и requests; ключ/сеть не нужны.

Сборщик теперь различает collected/no_new/all_sources_failed. Cached feed fallback
не устанавливает source health. Scheduler требует свежий collection-result.json,
healthcheck проверяет ok/status/возраст; NEWS_EVERY больше не перекрывается Docker CMD.

## Фактические ограничения и следующий шаг

- У всех 3012 оригиналов старого BGE экспорта URL отсутствует; сохранён null.
  Новые collector URL и цитаты подтверждены автономным HTTP-тестом.
- Неполные/неоднозначные группы не объявлены проверенными событиями. Explicit
  raw_fallback предупреждает о возможных дублях. Без него результаты deferred.
- Между разными repartition/merge operation стабильность canonical event ID не
  гарантируется; истории не объединяются молча.
- Это локальный прототип: прежний вход кабинета не является production
  аутентификацией; локальный company manifest должен поставляться доверенно.
- Graph/bank RAG не подставляются без данных. HTTP upload и чат нового persisted
  пути явно сообщают об отсутствии подключения; прежний demo API сохранён.
- Повторное применение идемпотентно для фиксированного as_of. Для следующего периода
  оператор меняет as_of; автоматическая политика расписания анализа не введена.
- Новые реальные BGE embeddings и live merge в этой приёмке не вычислялись:
  использованы проверенные исходные векторы, а атомарная замена с новыми vectors
  проверена fake encoder/настоящим MergeService. Повторная загрузка полной BGE модели
  не выполнялась: доступная RAM хоста на финальной проверке около 1,4 GiB; валидированы
  native artifacts, upstream encoder и синтетическая parity-регрессия.

Разрешённая live-проверка завершена; новых платных вызовов для приёмки не требуется.
Следующий минимальный продуктовый шаг: восстановить полный экспорт с URL и оценить
качество merge/аналитики на согласованном ручном эталоне в рамках отдельной задачи.
