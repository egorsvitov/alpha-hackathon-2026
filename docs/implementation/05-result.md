# 05A–05C: аналитик, контекст и offline workflow

Основа: `2453da0`, ветка `news-personalization`. Начато агентом `gpt-6.1-sol` medium; после исчерпания общего лимита Codex оркестратор завершил проверку, исправления и этот отчёт. QA/history имеют отдельный [05c-result.md](05c-result.md). Живая LLM не подключена: выполнена offline/fixture часть задания, самостоятельная финансовая semantic review — not_run.

## Реализованные интерфейсы

- `ports.py`: единые GenerationRequest/GenerationResult, QAReport, Services и Generator/Budget Protocols. Порты задают границу будущего 06A, но не являются действующим бюджетным шлюзом.
- `build_context(...) → RunContext`: проверяет B6, профиль и Relevance, identity/version/as_of, pinned hash, receipt и даты источников, availability/quality/eligibility. Все исходные claims/конфликты обязательны. Нужные features и причины ранкера сохраняются; bank hits исключаются целиком при byte/hit/quote лимитах. Нехватка места для обязательного контекста — context_overflow.
- `build_source_registry(snapshot, evidence_bundle)`: детерминированные IDs исходных цитат и банковских фрагментов с locator/hash/version. Метаданные и значения формирует код, а не генератор. Источники из excluded context не разрешаются в draft.
- `analyze_event(...)`: фиксированный Python workflow с сохранением шагов/артефактов, retrieval attempts 0–2, generation максимум два раза. Retry поиска меняет запрос и требует причины. Перегенерация после поиска использует тот же общий лимит. Ошибка QA не становится ready при исчерпании попыток.
- `answer_question(...)`: делегирует общему модулю questions; immutable child runs, restart, проверки company/role/current event и unverified user assumptions. Общий CLI открывает постоянный EventStore также для вопросов.

Источник B6 не пересказывается свободно: source_fact копирует исходный claim буквально, включая forecast/negation. Для проверки персонализации есть явно синтетические шаблоны rate + loan/deposit с условным why_important и next_step, неизвестными параметрами договора и проверяемыми source/feature refs. Это не финансовый reasoning модели. Для произвольных рисков/возможностей и позиции банка пустые секции получают insufficient_evidence; шаблон не выдаёт их отсутствие за доказанный вывод.

## Схемы и примеры

- `llm/agent/contracts/insight.v0.2.schema.json` — отдельная v0.2, прежняя v0.1 сохранена.
- `llm/agent/prompts/draft.schema.json`, system/user templates — ограниченный ответ генератора.
- `data/examples/personalization/agent/forecast-context.json`, `forecast-draft.json`, `insight.v0.2.json` — полностью синтетические примеры.
- [Миграция v0.1→v0.2](../insight-v0.2-migration.md) описывает новые refs, статусы, версии и QA.

Fixture всегда limited либо более строгий отказ. QA отдельно показывает structural_status, semantic_status, publication_status; semantic_status=not_run. Ready в этом режиме не выставляется. Live закрыт в коде независимо от ключа/allow flags до 06A. Число токенов/cost от реальной модели не моделируется: pricing_status=no_llm_calls.

## Проверки

Финальные команды и количества — в [общем отчёте этапа](04-05-orchestration.md). Ключевые наблюдения:

- Normal/forecast/conflict проходят с сохранённым текстом claims; оба конфликтующих варианта остаются в контексте.
- Оригинальный partial без даты остаётся deferred у ранкера. Для end-to-end partial использован отдельно обозначенный synthetic derivative с согласованными датами события и источников; исходный файл не менялся. Система не изобретает Relevance ради теста.
- Foreign refs и forecast drift исправляются одной repair-попыткой либо приводят к отказу без claims. Requested evidence вызывает реальный второй поиск с иным query; предел генераций соблюдается.
- Подмена bank quote/date/verification/synthetic flag/ID/corpus version блокируется до генерации. Непроверенный retrieval output сохраняется отдельно; failed steps видны в trace.
- Одна source новость при кредитном и депозитном профилях сохраняет одинаковые факты и даёт разные условные механизмы с соответствующими features. Эти профили получены из synthetic CSV настоящим CompanyRepository, а не подменой score по ID.
- CLI analyze → отдельный процесс ask работает; child ссылается на parent, parent не мутирует. Чужая компания/роль, withdrawn/update и context overflow блокируют ответ.

CLI-команды и аргументы: [llm/agent/README.md](../../llm/agent/README.md). Путь событий соответствует общему convention `fixture-events.sqlite3`; runs/artifacts хранятся локально в выбранном state-dir. В Git не добавляются реальные банковские quotes и профили.

## Оставшиеся ограничения

Независимый semantic reviewer, живой провайдер и общий бюджет/кэш/HTTP/UI принадлежат 06. QA имеет узкие проверяемые guards, но не универсальную проверку экономического смысла; положительный structural result этого не скрывает. Fixture-диалог поддерживает демонстрационный вопрос о неизвестных условиях договора, а не произвольные вопросы руководителя.

При повторном наблюдении сохранённого события changes_since_previous=unknown, если предыдущий результат не загружен для содержательного сравнения; escalation/deescalation автоматически не назначаются. Статусы и field-level provenance сохраняются, но автоматического анализа различий исторических выводов здесь нет. Клиентский доступ проверяется по company/role binding; полноценная авторизация — ответственность backend.
