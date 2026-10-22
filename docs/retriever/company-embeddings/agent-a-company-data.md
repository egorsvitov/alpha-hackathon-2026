# Агент A — профили, шаблоны и платёжный индекс

Модель: Codex Sol 6.1 Medium. Статус документа: задание для будущего запуска, не разрешение выполнять его сейчас. При запуске агент работает только после E0 из [integration.md](integration.md). Общий API — [contracts.md](contracts.md).

## Цель и выход

Построить детерминированные query texts компании и переиспользуемый индекс направленных платежей. Результат: CompanyViews, PaymentNeighborhood, отраслевые агрегаты и provenance. Не вычислять embeddings, не ранжировать новости, не вызывать LLM.

Первый milestone A1 — base views без графа. A2 — граф. B может начать интеграцию после A1, не ждать A2.

## Прочитать

- `docs/retriever/company-embeddings/{README,contracts,integration,evaluation}.md`.
- `docs/retriever/{architecture,implementation-plan,contract}.md`, особенно R2 и ограничения источников.
- `ranking/profiles.py`, `config/normalization.json`, `graph.py`, `profile_cli.py`, `tests/test_profiles.py`, `tests/test_graph.py`.
- `docs/company-data-and-news-research-context.md`, Q&A `data/task/Новостная аналитика для принятия бизнес-решений Альфа Хакатон.timestamps.txt`, интервалы 16:26–23:16.
- Заголовки трёх `data/companies/*.csv`; в текущей фазе planning их массово не читать. После разрешения execution использовать малые fixtures, реальный full graph build — только на этапе I4.

## Владение файлами

Разрешено создавать:

- `retriever/company_views.py`;
- `retriever/payment_store.py`;
- `retriever/graph_context.py`;
- `retriever/config/company-view-templates.v1.json`;
- `retriever/tests/test_company_views.py`, `test_payment_store.py`, `test_graph_context.py`;
- `retriever/tests/fixtures/company_data/*` — только синтетические малые CSV/JSON;
- `docs/implementation/company-embeddings-agent-a.md` — отчёт по реально выполненному.

Разрешено точечно менять `ranking/graph.py`, `ranking/profile_cli.py`, `ranking/tests/test_graph.py` только для batch-dependent version и conflict knownness. Не переписывать CompanyRepository, не менять профильную схему или root normalization и чужие tests.

Общие contracts/config dataclasses, root pyproject/lock/notices и CLI dispatcher принадлежат оркестратору. Список нужных dependency/config изменений направить ему, не редактировать самостоятельно.

## API и контракты

Реализовать ровно A API из contracts.md:

- `build_company_views(profile, *, policy) -> CompanyViews`;
- `build_universal_view(views, *, policy) -> CompanyView | None`;
- `PaymentGraphStore.build/open/neighborhood/affected_companies`;
- `build_sector_aggregates(neighborhood, *, repo) -> list[SectorAggregate]`.

Компания входит исходным dict CompanyProfile; не вводить собственный reader компаний. Связи people/GROUP_ID остаются отдельными необрабатываемыми типами; `group_id` сохраняется в исходном профиле, не добавляется в text и не влияет на graph.

## Шаги реализации

1. Проверить E0 dataclasses и sample fixtures. Если нет обязательных полей — DependencyNotReady и конкретный запрос оркестратору; не менять contracts локально. Снять git status и сохранить пользовательские изменения.
2. Реализовать text normalization и versioned templates строго по contracts.md. Поддержать отрасли вне ручного industry_ranges через исходный OKVED name. Отделить facts от условных search_topics. Ни один чужой ID/тип отношений не возникает из шаблона.
3. Построить content hash без timestamps/IDs/full source SHA; сохранить полные источники отдельно. Изменение неизвестного/false флага не должно превратить его в наличие продукта. Если text одинаковый, text_hash одинаковый для разных компаний.
4. Реализовать universal как конкатенацию base texts в фиксированном порядке, без LLM и без добавления его в default view set. Длина проверяется encoder B; A не режет произвольно строку.
5. Создать постоянную SQLite raw_edges с номером записи, payer/beneficiary, положительной Decimal string и count либо invalid marker. Переиспользовать `csv_rows`, `parse_decimal`, `decimal_string`, политику `ranking.graph.POLICY`. Не применять SQL SUM к Decimal string через float для финансовых totals: суммы считать Decimal в потоке соответствующей группы.
6. Повторы определять по ВСЕМ nonself raw rows до valid-фильтра. Исключать все записи repeated pair, включая valid+invalid. Self-loop отдельно исключить. Сохранить статистику и причины исключения. Построить retained_edges с индексами `(payer,beneficiary)` и `(beneficiary,payer)` и multiplicity/diagnostics. Build пишет временный файл, сверяет source SHA до/после, затем атомарно публикует. Повторный build для того же source+policy открывает проверенный индекс.
7. `neighborhood()` читает только соответствующие пары через индексы, не CSV и не full table scan. Уточнять статусы соседей по `repo.conflicts` и `repo.rows`, не просто company_ids. Нормализовать соседские профили лениво; не получать все 85k профилей для одного запроса. Cached profile lookups ограничить размером и source version.
8. Считать отдельные denominators incoming/outgoing по всем сохранённым рёбрам. Coverage по usable profiles и cleaning losses различать. Vector coverage не выдумывать — null до B. Empty direction → counts=0, fractions=null. Unknown profiles сохраняются как рёбра с unknown metadata.
9. Сформировать стабильный локальный graph_content_hash и отдельный provenance_hash. graph_content_hash не включает stats всех выбранных компаний, глобальный source SHA, batch size или номера CSV строк. Include локальные потери очистки и экономические metadata. Нужна одинаковая версия при запросе [A] и [A,B], а также при reorder строк с тем же смыслом.
10. Точечный fix legacy `attach_payment_graph`: убрать глобальный scan_statistics из company artifact, оставить его в result stats. Убедиться, что возвращённые агрегаты/feature refs и validate_contract прежние. `profile_cli` передаёт `repo.company_ids - repo.conflicts`; signature attach_payment_graph не менять. Полный source hash может по-прежнему менять legacy profile_version: новый embedding content cache от него не зависит.
11. Создать секторные агрегаты по raw ОКВЭД с refs каждого соседа/ребра и отдельными unknown totals. Не считать платежи закупкой/выручкой; не суммировать business facts соседей в профиль выбранной компании.
12. Реализовать reverse adjacency `affected_companies(changed_company_ids)` для обновления графовых representations при смене деятельности/статуса соседей. Возвращать и отправителей, и получателей; old/new edge delta merge делает orchestrator/B при смене dataset.
13. Заполнить отчёт и передать B примеры CompanyViews/PaymentNeighborhood с null vector coverage. Передать C fixture с графом, с неполнотой, без графа. Код в чужих файлах не исправлять.

## Unit и integration проверки

- Минимум cases: одинаковый ОКВЭД/разные loan flags → одинаковый business, разный finance; все false/unknown → no finance; неизвестная отрасль при известном OKVED name → business есть.
- FTS True без стран, только регион, страны+FTS False, смешанные UNKNOWN tokens, все unknown → корректные views/omissions/limitations. Нет импорта/экспорта/FX из страны.
- Все feature refs разрешаются в соответствующем CompanyProfile; profile не мутируется; другой source record меняет provenance, но не text/content hash.
- Старые `ranking.tests.test_profiles` и `ranking.tests.test_graph` проходят. Добавить batch-independence regression в test_graph.
- Fixtures платежей: self-loop; incoming/outgoing; один ID в обоих направлениях; duplicate valid+valid; duplicate valid+invalid; NaN/negative/fraction count; unknown/missing/conflict profile; пустое направление; сумма Decimal с дробями.
- Сравнить новый store с attach_payment_graph на этих fixtures: сохранённые рёбра, направления, amounts/counts/shares и cleaning loss равны. Разница graph_version/дополнительные metadata ожидаема, поля не подгонять под случайные runtime hashes.
- После build подменить `csv_rows` исключением: repeated `neighborhood()` продолжает работать. EXPLAIN QUERY PLAN подтверждает использование payer/beneficiary indexes. Массовая обработка не должна вызываться endpoint-ом.
- Ребро к конфликтующему ID существует, но этот сосед не usable. После изменения профиля соседа affected_companies содержит затронутых обоих направлений.
- metadata_source для RUB/period требует проверяемого manifest; другой fixture CSV без manifest возвращает unknown metadata.

## Definition of Done

- A1: все base view cases проходят; API совпадает с E0; 0 LLM/embedding вызовов.
- A2: один CSV ingestion на новый dataset, query без перечитывания CSV; parity tests проходят; batch-independent semantic graph hash.
- Старые профили/B6 API не сломаны; нет demo ID branching.
- После отдельного I4 допускается сверка full counts с pinned SHA из README (1 353 153 edges и coverage); при других hashes reference counts не использовать как истину.
- Переданы examples/provenance и отчёт. Статус качества рекомендаций не заявлен.

## Запреты и зависимости

Нельзя: менять CSV, писать векторную БД, обучать модель, вычислять GNN/node2vec, распространять риск по GROUP_ID/физлицам, менять B6/EventStore/аналитика, ставить hard threshold значимости, править файлы B/C. API changes — только через оркестратора.

Зависимости: E0 обязательный; B/C не нужны для unit-тестов. Секторные histogram и новые text graph views не включать автоматически в retrieval.

## Итоговый отчёт

`docs/implementation/company-embeddings-agent-a.md`: commit/base (если commits разрешены), изменённые файлы, API, команды/результаты тестов, фактические data hashes/counts только если build разрешён и выполнен, ограничения, зависимости B/C, нерешённые semantic questions. Отличать planned, implemented, tested, measured.
