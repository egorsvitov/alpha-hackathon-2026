# 03A + 03B: фактический результат

Дата: 2026-10-21. Реализованы оба последовательных этапа; CPU/offline, без API, GPU и LLM. B6 и четыре исходных файла `docs/b6-examples` не изменялись. Score — некалиброванный эвристический приоритет внимания, не вероятность и не денежный эффект.

## 03A: сигналы и кандидаты

`ranking.signals.prepare_signals(StoredEvent)` сохраняет claims, ranking_features, спорные `(claim_id, field_path)` и snapshot, не меняя bundle. `load_mappings` и `validate_mappings` исполняют восемь правил из спецификации: точные нормализованные aliases, claim-local selectors, типизированные company expressions, границы ОКВЭД, quality и geography policy. Спорный родитель/потомок dependency блокирует соответствующее основание; конфликт числового value не выключает безопасный тематический механизм и не даёт числовой inference.

`ranking.engine.candidate_report(snapshot, profile, *, as_of, rules, entity_mapping=None)` — внутренний отчёт с eligibility, reason_codes, age_policy, awards, diagnostics, limitations. Это не RankingResult и не готовая оценка. Все дублирующие matches и отклонённые модальности остаются diagnostics; совпадения только ranking_features имеют отдельный `metadata_match`, пустые claim IDs и **не получают claim awards**. Неизвестные labels остаются в сигналах, заголовок не служит selector. Forecast/plan/proposal сохраняют условность; opinion, unknown и negated не получают awards восьми правил.

Проверка 03A: `uv run --frozen python -m unittest ranking.tests.test_signals -v` — passed, 3 test methods, внутри матрица восьми правил с positive/negative, forecast, unknown/negated, unknown alias, selector/value conflicts, перестановкой/дублированием claims; отдельные geography/FTS и config-error пары.

## 03B: API, формула и выдача

`rank_events(snapshots, profile, *, role, as_of, rules, entity_mapping=None, top_k=5)` возвращает точный wrapper `{snapshot, relevance}` в ranked/feed. `load_rules()` загружает baseline config. API сначала отбирает доступные по receipt/producer-update snapshots, затем максимальную численную версию, затем availability/quality: withdrawn не воскрешает прежнюю active-версию. Дополнительно future source publication консервативно исключает весь snapshot. Undated publication deferred. Lookback 30 дней; upcoming до 14 дней и durable recheck ограничены 180 днями. Для durable нужны asserted/affirmed effective claim и известная дата; repeal/negation и спорные stage/date того же предмета блокируют fallback. `effective_at.end` не трактуется как прекращение действия.

Одна лучшая причина на группу, tie-break rule_id/claim_id; cap применяется до роли. `weighted_g = min(cap_g, best_points_g) * role_multiplier * 2**(-age_days/10)`, с floor=0.5 только для разрешённых fallback. `raw_total=sum(weighted_g)`, `score=min(1,raw_total/100)`, contribution распределяется из тех же групп. Raw total/диагностика сохраняются; нет бонуса за число claims/источников, величину извлечённого числа или свежесть без business signal. Правила/маппинги/resolver входят в rules_sha256; input_snapshot_hash включает event/version/payload hash/receipt/mode, исключает технический ingest_id. Mapping config читается один раз на rank run.

Сортировка score desc/raw_total desc/event_id asc. Feed применяет threshold 0.15, top-K и max_per_topic=2; override включается явно. Без оснований — deferred/unknown, а не доказательство неважности. Relevance хранит ограничения профиля/источников, claim/feature IDs и разрешаемые event paths.

Entity mapping — отдельная доверенная конфигурация, не извлекаемая из новости. Проверяются substantive roles; автор прогноза не объект. Synthetic mapping разрешён только для fixture snapshot со всеми synthetic sources. Direct match 65; graph clean_observed даёт 20+35×observed_share соответствующего направления, limited — 5 без использования доли, unavailable/not_loaded — 0. Доля описывает очищенный поднабор платежей, не выручку/зависимость. Дубликат прямого и собственного graph основания не начисляется.

CLI `ranking.rank_cli.register_parser` подключает `rank` и `demo` к общему CLI. Fixture-import использует только общий `load_bundles`, `ingest_bundles`, `FixtureClock`, временный EventStore; state replay читает `fixture-events.sqlite3`, live — `events.sqlite3`. Live запрещает events/fixture-clock. `--relevance-event-id` + `--relevance-output` извлекают реальный ranked Relevance; unscored возвращает `scored_pair_unavailable`. Demo pinned receipt: 2026-10-20T17:00:00+03:00.

Синтетические события: `data/examples/personalization/events.jsonl` (9 bundles, пять новых fuel/wages/steel/restriction/fx и четыре исходных синтетических примера); reusable fixtures в `ranking/tests/fixtures/events`. Явные synthetic sources/limitations, правильные Unicode spans. Валидация предупреждает original_text_not_checked, поскольку полного исходного article text нет; это не скрывается как верификация реальной новости.

## Приёмка

| ID | Статус и фактическое основание |
|---|---|
| R01 | passed: transport/IT mutation + root integration three profiles; без ID-specific правил |
| R02 | passed: fresh без применимого признака deferred, без score |
| R03 | passed: восемь forecast/negated/opinion/unknown пар, hold сохраняется в reason; plan/proposal допускаются тем же явным DSL policy |
| R04 | passed: selector conflict блокирует; value conflict сохраняет limited механизм, числового бонуса нет |
| R05 | passed: 10 эквивалентных claims и перестановка не меняют score; store отвергает version collisions |
| R06 | passed: role, unresolved mapping, descendant participant conflict, synthetic live prohibition |
| R07 | passed: contract validation, сумма contributions, claim event pointers и feature IDs |
| R08 | passed: unknown/future/withdrawn/upcoming + receipt/producer selection и future source cutoff |
| R09 | passed: role/caps, stable order, top-K, diversity/explicit override; ranked отдельно от feed |
| R10 | passed: root cross-module CLI fixture↔API exact equality, ingest state→rank, receipt cutoff, live rejection |
| R11 | passed: clean .8 → 48 raw points, limited → 5, unavailable → 0; direction preserved |
| R12 | passed: product cap 30×1.2=36, saturation raw_total различает score=1, contributions sum |
| R13 | passed: durable 40 дней/floor/limited, repeal/forecast/negated/date-conflict блокируют; 181 дней refresh; обычный entity в 30 дней =0.08125 |
| R14 | passed: все восемь rules, positive/negative mutation matrix, поля только конкретного claim, title alias не включает правило |

Команды:

```bash
uv run --frozen python -m unittest discover -s ranking/tests -v
# passed: 52 tests (включая 01/02 и cross-module tests root)
uv run --frozen python -m unittest ranking.tests.test_signals ranking.tests.test_ranking ranking.tests.test_personalization_integration -v
# passed: 16 test methods после последней оптимизации config loading
uv run --frozen python -m ranking demo --as-of 2026-10-20T18:00:00+03:00 --output-dir /tmp/alpha-ranking-demo
# passed, 3 schema-valid fixture outputs
```

Demo: LOGISTICS ranked=4/feed=3/deferred=5; IT 1/0/8; TRADE 5/3/4; excluded=0 на этих девяти актуальных fixtures. Saturation rate: 0/10 scored pairs =0%; это наблюдение fixtures, не целевая метрика качества. Исключения/durable/saturation проверяются отдельными mutation tests. Интеграция root подтверждает один EventStore → три профиля → CLI/API с одинаковыми snapshot hashes и применимыми разными reasons.

## Ограничения и передача 04/05

Independent expert quality evaluation, real-data news accuracy, live network/LLM, HTTP/UI, RAG/agent — **not_run**, вне 03. Графовые scoring tests проверяют quality gate на контролируемом graph shape; 02 отдельно тестирует реальный attach_payment_graph, а основной demo имеет not_loaded graph. Лексический fallback ограничен минимум двумя содержательными совпадениями claim text/activity name и не доказывает влияние. Нет исторического business snapshot профиля, поэтому историческая применимость unknown отражена в limitations. Нет автоматического entity resolver, мониторинга полного реестра действующих санкций, magnitude bonus или seen_versions.

04/05 должны извлекать Relevance из `ranked[*].relevance` вместе с matching snapshot и pinned profile; basis conditional_mechanism/metadata_match/lexical_match нельзя повышать до source_fact. raw_total/score не являются confidence. Forecast/polarity/action/stage сохраняются в claim refs и reason text; durable_recheck требует проверки актуальности, не заявления «ограничение действует сейчас». Все sources и feature refs подлежат последующей проверке по исходным snapshots.

Дополнительная интеграция оркестратора после сдачи 03: `test_actual_graph_output_controls_counterparty_ranking` проверяет настоящий attach_payment_graph → schema-valid profile → synthetic resolver → rank, без подставного graph shape. Одинаковая доля 0.8 при clean/limited качестве даёт 48/5 raw points. Финальный общий прогон — 53 теста, passed; подробнее в [общем отчёте](00-orchestration.md).
