# RAG: подготовка документов и поиск

Здесь OCR PDF, разбиение на фрагменты, метаданные дат/страниц, индекс и retrieval. Все исходники и результаты находятся в `data/`.

Старые команды сохранены для воспроизводимости прежних probes:

```bash
python3 llm/rag/prepare_rag.py
python3 llm/rag/search_rag.py "золото покупки центробанков" --topic gold
python3 llm/rag/search_rag.py "прогноз ставки" --topic policy_rate --as-of 2026-09-14
python3 llm/rag/verify_artifacts.py
```

Подготовка использует Poppler и Tesseract `rus+eng`. Поиск по сохраненному корпусу требует только Python. Без `--include-historical` обзор стали 2021 года исключен; `--as-of` также исключает документы с неизвестной датой выпуска.

Выход поиска: фрагменты, ID, PDF-страницы, даты и показатели поиска. Это кандидаты на доказательства; OCR и смысловое соответствие необходимо проверять. Полноценной генерации ответа в этом модуле нет.

## EvidenceBundle v0.1

Новый API: `Corpus(path, config=None, base_dir=None, catalog_path=None, ocr_dir=None)` из `llm.rag.corpus`; `load_config()` и `retrieve_evidence(bundle, profile, relevance, *, as_of, corpus, config, attempt=0, retry_reason=None)` из `llm.rag.retriever`. Необязательные параметры Corpus передаются по имени. `config={}` использует настройки закреплённого корпуса. `.version`/`.corpus_version` включает содержимое, metadata/exclusions, manifest, проверки происхождения и версию индекса. Изменение файлов требует создания нового Corpus. Индекс строится при загрузке, повторный поиск не читает PDF/OCR. `.get_chunk(id)` и `.verify_hit(hit)` доступны независимому QA.

Corpus проверяет JSONL, уникальность ID, Unicode-диапазоны, SHA-256 текста, даты/метаданные. При доступных исходниках проверяет PDF SHA-256, номера страниц по локальному manifest и точное совпадение цитаты с OCR page. Каталог рядом с корпусом задаёт даты/exclusions. Недоступный PDF/OCR ограничивает provenance. Автоматическая проверка OCR-текста не является визуальной проверкой оригинала и чисел. Статус visually_checked_excerpt требует record именно для этих hash/page/boundaries с reviewer/method/reviewed_at.

Подготовить Relevance настоящим ранкером для синтетической пары, затем выполнить поиск:

```bash
uv run --frozen python -m ranking rank --events data/examples/personalization/events.jsonl --mode fixture --fixture-clock 2026-10-20T17:00:00+03:00 --profiles data/examples/personalization/profiles.json --company-id DEMO_TRADE --as-of 2026-10-20T18:00:00+03:00 --relevance-event-id demo:rate-hold --relevance-output data/processed/personalization/relevance-demo.json --output data/processed/personalization/ranking-demo.json
uv run --frozen python -m llm.rag retrieve --event docs/b6-examples/normal.json --profiles data/examples/personalization/profiles.json --company-id DEMO_TRADE --relevance data/processed/personalization/relevance-demo.json --as-of 2026-10-20T18:00:00+03:00 --corpus data/processed/rag_chunks.jsonl --output data/processed/personalization/evidence.json
uv run --frozen python -m unittest discover -s llm/rag/tests -v
```

Для чистого checkout заменить `--corpus` на `llm/rag/tests/fixtures/corpus.jsonl`: он полностью синтетический. Пример EvidenceBundle — `tests/fixtures/evidence.json`; схема — `docs/contracts/evidence-bundle.schema.json`, Draft 2020-12 с FormatChecker. CLI проверяет входные контракты и соответствие Relevance выбранным event/profile. JSON пишет в stdout либо output, ошибки — stderr. Исходные PDF разрешаются относительно корня проекта либо `base_dir`; OCR — относительно каталога корпуса либо `ocr_dir`.

Каждый вызов выполняет ровно `attempt=0|1|2`; для `attempt>0` необходим `retry_reason`. Повторы инициирует orchestrator. Попытка 1 снимает topic-фильтр, 2 уточняет динамику основного объекта; обе сохраняют даты/exclusions. В query_plan один entry с queries, фильтрами, temporal mode, причиной и версиями. Кредитный механизм добавляется только при явном указании в Relevance. Числа конфликтующих источников не становятся query anchor.

Строгий default допускает date-only документ с начала следующего дня Europe/Moscow. `daily` сравнивает календарные даты и ставит `intraday_availability_unproven`. `research_current` сохраняет недатированные/исторические документы исключительно в diagnostics. Явное `include_historical=true` в strict/daily допускает исторический контекст с маркировкой. Неизвестная дата не выводится из имени файла. Дата документа не доказывает горизонт прогноза.

NFKC/casefold/ё→е и явные экономические формы версионируются в config, новых зависимостей нет. Admission до top-K требует положительный BM25 и предметный concept либо два содержательных lexical совпадения. Общие слова/регион не заполняют выдачу. Дедупликация по диапазонам страницы, лимиты документа/контекста применяются без обрезания уже созданной цитаты.

`ok` означает пригодные поисковые кандидаты; `partial` — кандидаты с ограниченным OCR/происхождением/применимостью; `no_bank_evidence` — пригодных кандидатов не найдено; `unavailable` — файл корпуса отсутствует. Загруженный пустой корпус имеет SHA-256 version и `empty_corpus`, отсутствующий — null version. Ни один статус не подтверждает bank_view и не доказывает отсутствие мнения банка. Retrieval score не является вероятностью истинности.

Старый `search_rag` возвращает raw BM25 hits с прежней нормализацией, без нового admission/строгой intraday-политики; его результат нельзя передавать аналитику как EvidenceBundle. Старый gold probe без as_of допускает неизвестную дату; новый retriever этого не делает. `prepare_rag.py` сохранён, повторный OCR для поиска не требуется.
