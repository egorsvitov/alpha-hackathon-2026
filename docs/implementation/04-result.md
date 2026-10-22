# 04 — банковский RAG

Исходный commit: `2453da0`, рабочая ветка `news-personalization`. Реализованы 04A и 04B локально, без LLM, API, скачиваний и повторного OCR. Новые зависимости не добавлялись. Чужие файлы ranking/agent и общие конфиги не изменялись.

Результат: `Corpus(path)` с проверкой корпуса, memory BM25, версионируемые mapping/tokenizer/admission/temporal policy, одна ограниченная попытка `retrieve_evidence` по INTERFACES, EvidenceBundle schema 0.1, retrieve CLI, синтетические fixtures и тесты. Код в `llm/rag/{corpus,query,retriever,__main__}.py`, `config/`, `tests/`; схема `docs/contracts/evidence-bundle.schema.json`; смысловые запросы `data/examples/personalization/rag-queries.json`. Старые prepare/search/verify сохранены.

Передача 05: `from llm.rag.corpus import Corpus`; `from llm.rag.retriever import load_config, retrieve_evidence`. `config={}` использует настройки Corpus. `.get_chunk(chunk_id)` возвращает исходный locator, `.verify_hit(hit)` проверяет точные document/page/source-path/boundaries/quote/hash. Одна попытка 0–2, retry_reason обязателен после нулевой, query_plan содержит один фактически выполненный entry. Отсутствующий корпус имеет null version; загруженный пустой — SHA-256 version/no_bank_evidence/empty_corpus. Полностью синтетический корпус: `llm/rag/tests/fixtures/corpus.jsonl`, пример результата: `evidence.json` рядом. Это вымышленные материалы, не банковские цитаты.

Фактически выполнено:

- `uv run --frozen python -m unittest discover -s llm/rag/tests -v` — 17 тестов passed. G01 mapping rates→policy_rate и русские формы; G02 pre-ranking future/unknown/history/exclusions; G03 граница следующего московского дня и daily limitation; G04 content/date/exclusion/catalog version; G05 Unicode quote/hash/bounds, повреждённые PDF/OCR; G06 overlapping chunks и контекстный бюджет; G07 missing/empty/pending OCR; G08 разные bounded retries, неизменные temporal guards. Дополнительно weak BM25 rejection, lexical fallback с двумя словами, exact manual record guard, config default, duplicate/date rejection, отсутствие повторного чтения источников.
- Draft202012Validator.check_schema и FormatChecker — schema и каждый результат тестов валидны; synthetic evidence fixture также валидирован и его quote проверена Corpus.verify_hit.
- Команда подготовки relevance из README через `python -m ranking rank ... --relevance-event-id demo:rate-hold --relevance-output ...` — passed, Relevance получена настоящим ранкером для явно синтетического события и профиля.
- `uv run --frozen python -m llm.rag retrieve` с аргументами README для реального локального корпуса — passed, `partial`, 5 кандидатов; с synthetic corpus — passed, `partial`, 1 synthetic candidate. Артефакты только в ignored `data/processed/personalization/`.
- `uv run --frozen python llm/rag/verify_artifacts.py` — существующие пять вручную подобранных smoke-probes passed, исходная provenance/temporal/ref проверка passed. Этот legacy скрипт самостоятельно пишет `json_schema...=not_run`; схема нового EvidenceBundle отдельно проверена выше. Legacy probe без as_of допускает undated gold, поэтому он не является тестом новой строгой temporal policy.

Полный локальный корпус: 109 chunks, 84 retrieval eligible. Для всех 109 автоматически подтверждены SHA-256 доступного PDF и `OCR_page[start:end] == text`; page bounds дополнительно проверяются по существующему PDF manifest. Это проверка воспроизводимости извлечённого текста. Визуальная проверка исходного PDF/чисел не выполнялась. OCR остаётся pending_review, даже когда date_status ссылается на проверенную страницу заголовка.

Дополнительные новые strict replay probes на `2026-10-20T18:00:00+03:00`:

| Topic | Статус | Кандидатов | Наблюдение |
|---|---|---:|---|
| rates | partial | 5 | Среди кандидатов policy rate обзор, страница 1 |
| inflation | partial | 5 | Inflation tracker, страницы 1 и 2 |
| labor | partial | 5 | Макростатистика/потребление, страницы 1–2 |
| gdp | partial | 4 | Макростатистика 1П26, страницы 1–2 |
| gold | no_bank_evidence | 0 | Неизвестная дата остаётся в diagnostics |
| steel | no_bank_evidence | 0 | Исторический обзор исключён default |

В каждом strict probe исключено 25 retrieval-ineligible, 8 historical-only и 8 eligible undated chunks до ранжирования. Никакие недатированные gold или исторические steel не добавлялись ради результата. Эти probes придуманы разработчиком, не являются независимым benchmark качества retrieval. Старый шестибуквенный tokenizer сохранён исключительно в legacy CLI; новый API использует явные экономические формы.

Ограничения: локальный BM25, отсутствие независимой оценки смысловой поддержки; top-K содержит кандидатов, не доказанную позицию банка. Retry 2 уточняет динамику объекта, не выбирает заранее победивший конфликтующий факт. Проверенный горизонт отсутствует в текущих исходных chunks, поэтому каждый hit несёт `forecast_horizon_unverified`. Недоступное происхождение и pending OCR дают partial. Промежуточные схемы и registry дальше проверяются QA/аналитиком; банковская позиция не выводится из одного retrieval status. Отдельная визуальная экспертиза оригиналов, live модель, HTTP/UI и independent retrieval benchmark — not_run. Usage/cost: `no_llm_calls`, 0.
