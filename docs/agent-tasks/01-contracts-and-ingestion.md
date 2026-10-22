# Задание агенту 01: контракты и приём EventBundle

Реализуй это задание в репозитории хакатона, в рабочей ветке персонализации. Перед изменениями прочитай [COMMON.md](COMMON.md), [INTERFACES.md](INTERFACES.md) и текущий статус Git. История чата для выполнения не требуется. Доведи код до проверяемого результата, не ограничивайся проектированием.

## Цель и границы

Создать основу, на которую смогут независимо опираться профили, ранкер и аналитик: формальные JSON-контракты, строгий загрузчик EventBundle, проверку ссылок/смысла структуры, сохранение версий и воспроизведение на дату. Не реализовывать ранжирование и LLM; не изменять внешний формат B6.

B6 пока представлен файлами. Он отдаёт событие для всех компаний, с claims/evidence/conflicts. Существующие четыре примера должны остаться неизменными и валидными.

## Два последовательных выполнения

- **01A — контракты и проверка входа:** шаги A/B, схемы CompanyProfile/Relevance/RankingResult, их fixtures, decoder и validator. Завершить C01–C05 и проверки source flags ниже; после этого зафиксировать интерфейсы в отчёте. EventStore на этом этапе не нужен.
- **01B — хранение и загрузка:** шаг C, StoredEvent, SQLite, ingest CLI и C06–C10. Начинать после приёмки 01A; завершить жизненный цикл версий и общий ingestion-путь для 03.

Можно поручить агенту только 01A либо только 01B с этим же файлом. После каждого этапа запускать его проверки и обновлять `01-result.md`, явно перечисляя оставшиеся этапы. Полное задание 01 выполнено после обоих этапов. Схемы EvidenceBundle и Insight создают владельцы 04 и 05 по INTERFACES.md: они не блокируют приём новостей и ранкер.

## Прочитать код и данные

- `docs/b6-event-contract.md`, `docs/contracts/event-bundle.schema.json`, все `docs/b6-examples/*.json`.
- `scripts/review_event_bundle.py`: исследовательские проверки; перенести полезные идеи, но не выдавать этот скрипт за готовый runtime-валидатор.
- `llm/agent/contracts/insight.schema.json` и пример v0.1.
- `frontend/src/types.ts`, разделы совместимости в `docs/news-personalization-review.md`.
- `.gitignore`, существующие Python-зависимости backend. Пакет ML создаётся отдельно и не ломает backend lock.

## Что создать

| Файл/область | Результат |
|---|---|
| `pyproject.toml`, `uv.lock` | Минимальная воспроизводимая среда ML, включая JSON Schema; явный package discovery |
| `ranking/__init__.py`, `ranking/contracts.py` | Общие dataclasses/ошибки, canonical JSON/hash/time helpers |
| `ranking/b6.py` | Decoder, load_bundles, validate_bundle, проверки JSON Pointer |
| `ranking/event_store.py` | SQLite store версий и transactional ingestion |
| `ranking/__main__.py` | Подкоманды validate-b6 и ingest; расширяемый CLI |
| `docs/contracts/company-profile.schema.json` | CompanyProfile v0.1 |
| `docs/contracts/company-profiles.schema.json` | Envelope списка profiles для CLI/fixtures |
| `docs/contracts/relevance.schema.json`, `ranking-result.schema.json` | Relevance и результат списка |
| `data/examples/personalization/contracts/` | Минимальные явно синтетические валидные примеры новых схем |
| `ranking/tests/test_b6.py`, `test_event_store.py`, `test_contracts.py` | Проверки граничных случаев и схем |
| `docs/implementation/01-result.md` | Отчёт и инструкции следующему агенту |

Обновить notices для добавленных зависимостей. Добавить игнорирование локального state и производных ранкера. Не переносить `data/processed/ranking/` в fixtures — там могут быть реальные профили.

## Шаг A. Схемы и типы

1. Реализовать shape из INTERFACES.md, запретить дополнительные поля там, где перечисление фиксировано.
2. Допускать расширяемые строковые метки B6; enum topics/event_type остаётся таким, как в исходной схеме.
3. Для нового профиля сохранить singular-ключи loan/deposit/guarantee, добавить provenance и неизвестность. Не копировать UI-профиль как внутренний.
4. Разделить null score/unknown level и нулевой score/low level. Проверку суммы contributions реализовать межполевой функцией, не надеяться на JSON Schema.
5. Зафиксировать поля core-схем по INTERFACES.md, включая вложенные Origin, graph coverage и scoring diagnostics. EvidenceBundle/Insight здесь только прочитать как будущих потребителей; схемы и миграция Insight — результаты 04/05.
6. Создать минимальные непротиворечивые примеры со всеми обязательными полями. Примеры — ручные fixtures, не «вывод живой модели».
7. Вынести ошибки в `ValidationIssue(code, path, message, severity)`; path — JSON Pointer. Неструктурный текст ошибки не должен быть единственным контрактом для клиента.

## Шаг B. Загрузчик и валидатор

JSON object в `.json`; JSONL в `.jsonl`, один bundle на непустую строку. Ошибка содержит номер строки. Ограничить размер входа конфигом, документировать разумные стартовые пределы; не обрезать JSON молча. Отклонять duplicate JSON keys и нечисловые JSON constants.

Включить Draft202012Validator и FormatChecker. После успешной схемы выполнить отдельные проверки:

1. Уникальны claim_id, source_id, evidence_id, conflict_id; measurement_id уникален в bundle. Одинаковая article_id/version не описана двумя разными source_id с конфликтующими метаданными.
2. Каждая evidence.source_id разрешается. `start < end`, `end-start == len(quote)` в Unicode code points.
3. Если цитируется title, проверить точное равенство slice(source.title). Для text без исходного текста проверить только структуру и выдать статус `original_text_not_checked`; не заявлять точное равенство полному оригиналу.
4. Каждая claim.evidence_ids и field_evidence ссылка существует. Все evidence одного SourceClaim относятся к одному source_id, то есть одной версии статьи: это явное правило B6. Несколько цитат этой статьи допустимы. Другая статья даёт отдельный claim; claims могут входить в одно событие и связываться duplicate_claim/conflicts. Cross-source evidence внутри одного claim отклоняется с `claim_cross_source_evidence`. field_evidence ids входят в claim.evidence_ids.
5. Реализовать RFC 6901: `~1`, `~0`, массивы, недопустимый индекс, ошибочные escape. JSON Pointer должен указывать на существующее поле/элемент, не «приблизительно похожий» путь.
6. field_evidence покрывает непустые извлечённые поля claim либо их родительский объект: text, известные modality/polarity, непустую stage/attribution/action/object, участников/географию, непустые time/measurement-поля. Не требовать отдельную цитату для null, пустого массива и unknown-сентинела, обозначающего отсутствие знания. Технические IDs и сам field_evidence не требуют evidence. Это структурное покрытие; смысловую поддержку цитат проверяет QA.
7. Даты интервалов упорядочены, диапазоны Decimal упорядочены, qualifier соответствует типу значения. Не сравнивать десятичные строки лексикографически.
8. first_published_at совпадает с минимумом известных дат current sources или null, если они все неизвестны. Не отождествлять его с датой occurred_at.
9. conflicts.variants ссылаются на существующие claims и поля; нет повтора одной пары claim/path внутри конфликта. Не требовать разного числового значения для любого типа конфликта: противоречие может быть семантическим.
10. duplicate_claim связывает два разных текущих claim; updates начинается с текущего claim, старый target может быть внешним архивным ID; related_stage связывает события и не заставляет загрузить чужую историю. Отклонять self-loop, но не создавать обязательный сетевой обход.
11. Проверять противоречия content_kind/is_synthetic/is_full_original_text. Не выводить synthetic из user_provided или из is_full_original_text=false. Если сочетание не запрещено текстом контракта явно, это warning/вопрос владельцу B6, а не самовольно добавленный hard rejection.
12. Если producer updated_at раньше известных дат публикации текущих источников, выдать timestamp_consistency warning. Не исправлять timestamps и не ужесточать JSON-контракт без согласования; пригодность на as_of всё равно ограничивается фактическими датами.

Граница error/warning для source flags: запрещённая комбинация отклоняется только при наличии конкретного ограничения JSON Schema или цитируемого пункта `docs/b6-event-contract.md`. Текущая схема задаёт типы флагов, но не межполевые зависимости: `is_synthetic="true"` — schema error, а `content_kind=original, is_synthetic=true` — warning `source_flags_ambiguous`, не самовольно запрещённый объект. `user_provided` и неполный оригинал сами по себе не определяют синтетичность. Warning не блокирует put; он сохраняется в validation-отчёте/ingestion metadata, вне payload B6.

Качество/доступность — не ошибка валидности: withdrawn или insufficient может иметь пустые claims. Корректный объект должен храниться, даже если его нельзя анализировать. Прогноз в будущем также не является ошибкой.

## Шаг C. EventStore

Использовать стандартный sqlite3 с транзакцией и уникальным `(event_id,event_version)`. Таблица хранит canonical payload, hash, received_at и source_mode. Путь БД задаётся извне; тесты в tempfile. Создание/миграция идемпотентны.

`put` сначала валидирует bundle, затем атомарно:

- если ключ отсутствует — вставляет и возвращает inserted;
- если ключ/хэш совпадает — unchanged, received_at первого наблюдения не перезаписывается;
- если ключ совпадает, хэш иной — event_version_collision, исходная запись остаётся;
- новая более старая версия допустима для архива, но не откатывает latest.

`event_version` по B6 — положительное JSON integer, не строка, bool или semver. Сравнивать численно; в SQLite хранить INTEGER. `get_latest(event_id, as_of)` рассматривает только snapshots, где received_at и producer updated_at не позже среза. Выбирает максимальный event_version; затем downstream смотрит withdrawn/quality. Не фильтровать withdrawn до выбора версии. При отсутствии доступного снимка — event_not_found.

Проверку published_at для ленты выполняет ranking policy. Store хранит семантически валидные snapshots с будущими публикациями отдельно, не переписывая даты. Порядок поступления и физический порядок строк БД не должны менять выбор версии.

CLI импорт JSONL по умолчанию transactional на весь batch: сначала валидировать всё; при любой ошибке ни одной частичной записи. Зафиксировать эту семантику в README. Частичный batch-режим не нужен для MVP.

Время получать из переданного clock, а не разбросанных `datetime.now()`. Для demo — отдельный режим `--fixture-clock`, разрешённый только с `--mode fixture`, и отдельная БД. В live received_at не принимать от новости.

Общий `ingest_bundles` из INTERFACES.md используется и CLI ingest, и fixture-импортом rank/demo. Время для всего batch назначается одним вызовом injected clock, validation и запись атомарны; повторная загрузка сохраняет первое received_at. CLI `rank --events` разрешён только с `--mode fixture --fixture-clock <timestamp>` и импортирует B6 через этот же путь в изолированную БД. Live rank читает `--state-dir` после обычного ingest, не принимает импортные часы. Ни один downstream-модуль не создаёт StoredEvent вручную, подставляя дату публикации вместо receipt-time.

## Интерфейс CLI и ожидаемые проверки

После реализации должны работать:

```bash
uv sync --frozen
uv run --frozen python -m ranking validate-b6 --input docs/b6-examples/normal.json
uv run --frozen python -m ranking ingest --input docs/b6-examples/normal.json --state-dir data/processed/personalization/demo --mode fixture --fixture-clock 2026-10-20T11:00:00+03:00
uv run --frozen python -m unittest discover -s ranking/tests -v
```

validate-b6: JSON отчёт, exit 0 для валидного объекта, ненулевой exit для ошибки. ingest повторяется без создания второй версии; stdout содержит inserted/unchanged и snapshot ID, а не весь чувствительный payload.

## Приёмочные кейсы

- C01–C10 из [матрицы](ACCEPTANCE.md).
- Все четыре B6 fixtures проходят schema/semantic validation.
- В mutation-тестах сломать по одному: source ref, evidence ref, measurement range, time range, duplicate ID, quote end, field pointer, field coverage, тип source flag. Убедиться, что отказ связан с нужным кодом. Отдельно проверить warning для допустимого, но сомнительного сочетания source flags и успешный put такого bundle.
- Несколько evidence одной статьи принимаются; evidence двух sources внутри одного claim отклоняются; два source-specific claims в одном событии принимаются.
- Проверить emoji в quote: Python len и JS code units различаются; expected span по code points.
- Unknown расширяемый factor проходит; неизвестный закрытый topic — нет.
- В active/complete пустой claims запрещён схемой; withdrawn с пустыми claims допустим.
- ingest v2→v1 оставляет latest v2; v3 withdrawn подавляет v2; v4 active восстанавливает; historical срез до received_at v3 возвращает v2.
- v9→v10→v2 выбирает v10; строка `"10"` и bool отклоняются как event_version. Поведение CLI fixture-import и прямого ingest совпадает при одинаковых данных/часах.
- После закрытия/открытия БД всё сохраняется; два одновременных put одного ключа не создают дубль.
- Неизвестный ID и известный ID до первого received_at возвращают event_not_found.
- Запуск unit-тестов не читает raw CSV/PDF и не требует сети/LLM.

## Завершение

Зафиксировать созданные схемы и функции в отчёте; дать следующему агенту один валидный профиль-fixture и способ читать snapshots. Не объявлять NLP producer реализованным. Если обнаружено противоречие внешнего контракта, не «чинить» B6 молча: выделить issue и продолжить совместимую часть.
