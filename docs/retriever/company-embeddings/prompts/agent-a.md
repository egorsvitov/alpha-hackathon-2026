# Задание агенту A: company views и платёжное окружение

Передача этого промпта пользователем как задания на выполнение означает разрешение реализации в описанном scope; повторное подтверждение для кода и малых тестов не требуется. Простое чтение файла при планировании таким разрешением не является. E0 и интеграционные gates остаются обязательными.

Модель исполнения: gpt-6.1-sol, reasoning medium. Рабочий репозиторий `/home/pavel/projects/hackathons/alpha`. Это промпт для будущего запуска после разрешения реализации; текущая подготовка документов запуском не является.

Ты отвечаешь за данные для Impact Retriever: три детерминированных текста компании и постоянный индекс направленных платежей. Компания ищет новости; embeddings являются query, индекс содержит events. Кодирование/ранжирование/LLM не входят в твою работу.

Сначала прочитай:
- `docs/retriever/company-embeddings/agent-a-company-data.md` — полное твоё ТЗ;
- `docs/retriever/company-embeddings/contracts.md`, `integration.md`, `README.md`;
- `ranking/profiles.py`, `graph.py`, `profile_cli.py`, соответствующие tests и normalization config;
- `docs/retriever/architecture.md`, `implementation-plan.md` (R2), `contract.md`.

Обязательный вход: оркестратор завершил E0 и зафиксировал representation_contracts/ports. Если их нет, сообщи точную недостающую зависимость, не изобретай свою схему. Работай по двум поставкам: A1 base views, A2 graph; не жди готовности B/C для unit-тестов.

Твои новые файлы: `retriever/company_views.py`, `payment_store.py`, `graph_context.py`, `config/company-view-templates.v1.json`, `tests/test_company_views.py`, `test_payment_store.py`, `test_graph_context.py`, `tests/fixtures/company_data/*`. Точечные изменения разрешены только ещё в `ranking/graph.py`, `ranking/profile_cli.py`, `ranking/tests/test_graph.py` для batch-version/conflicting-ID fixes. Отчёт — `docs/implementation/company-embeddings-agent-a.md`. Остальные файлы принадлежат оркестратору/B/C; root dependencies/notices не менять.

API: `build_company_views(profile, *, policy) -> CompanyViews`, `build_universal_view(views, *, policy)`, `PaymentGraphStore.build/open/neighborhood/affected_companies`, `build_sector_aggregates(neighborhood, *, repo)`. Поля/типы строго contracts.md. Переиспользуй CompanyRepository и его features. Business — ОКВЭД/name; finance — только True продукты; trade_geo — подтверждённая ВЭД/страны/регион регистрации. Нет данных → view отсутствует. Неизвестное не превращается в False или наличие продукта. Нет LLM. Source provenance отдельно от semantic content hash.

Payment store: один ingestion на source version, SQLite с индексами payer и beneficiary; self-loops исключить, ВСЕ повторяющиеся directed pairs исключить до фильтра invalid. Decimal amounts и positive integer counts. Known IDs исключают repo.conflicts. incoming/outgoing имеют независимые denominators по ВСЕМ сохранённым соседям, включая неизвестных. Coverage от чистоты данных отличается от coverage профилей. Не называть платежи закупками/выручкой. GROUP_ID/физлица не являются платёжными рёбрами и не обрабатываются здесь.

Стабильность: одинаковое локальное соседство даёт graph_content_hash независимо от batch size и source row order; global source SHA/records остаются в provenance. Убери scan_statistics из per-company artifact legacy graph (оставь их в stats) и исправь набор known IDs в CLI; API/профильную схему не меняй.

Проверки: все refs разрешаются; identical text→identical hash; чужая CSV строка не меняет content; unknown/false/FTS conflict; graph parity с существующим attach_payment_graph на маленьких fixtures; duplicate valid+invalid; self-loop; incoming/outgoing; conflict ID; no CSV scan при повторном neighborhood; stable batch hash; все прежние profile/graph tests. Не запускай полный датасет до отдельного integration gate I4. Не меняй реальные CSV.

DoD: A1 отдаёт валидные views без LLM; A2 — indexed neighbors, sector aggregates, coverage/provenance; тесты проходят; примеры переданы B/C. Никаких заявлений об улучшении Recall. В отчёте перечисли файлы, команды и результаты тестов, implemented/planned различия, известные ограничения и dependencies. Не коммить/пушь без отдельного указания.
