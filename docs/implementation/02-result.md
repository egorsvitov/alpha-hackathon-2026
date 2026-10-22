# 02A + 02B: профили и наблюдаемые платежные связи

Исходный commit: `2453da0e72fe8e78a302391527edaf278d046cb2`, рабочая ветка `news-personalization`. Реализовано в общей ветке без переключения/слияния baseline. Старые `profiles.py`, `graph.py`, `validate_data.py` и тесты просмотрены через `git show`; прежние float revenue, dot-path features/source, неполная версия и формат synthetic не перенесены.

## Интерфейсы и файлы

- `CompanyRepository(csv_path, *, normalization_config=DEFAULT_CONFIG, imported_at=None, source_manifest=None, is_synthetic=False)`: один индекс CSV на экземпляр; `company_ids`, `record_count`, `conflicts`; `get(company_id) -> CompanyProfile` либо `ProfileError` со стабильным `code`.
- `attach_payment_graph(profiles, known_company_ids, payments_path, *, config=None) -> (new_profiles, stats)`. `config.state_dir` задаёт каталог локальных артефактов; другие настройки входят в версию правил. Исходные профили не изменяются.
- `ranking/profile_cli.py:register_parser(subparsers)` регистрирует общий CLI hook `args.func(args) -> int`. Общий `__main__.py` принадлежит заданию 01.
- Код: `ranking/profiles.py`, `ranking/graph.py`, `ranking/profile_cli.py`, `ranking/config/normalization.json`; тесты: `ranking/tests/test_profiles.py`, `test_graph.py`; полностью вымышленные CSV: `ranking/tests/fixtures/profiles/`.
- `data/examples/personalization/profiles.json`: schema-valid envelope с DEMO_LOGISTICS, DEMO_IT, DEMO_TRADE, `is_synthetic=true`, граф `not_loaded`. Это вымышленные компании и значения, не банковские обезличенные записи.

Пути относительно корня проекта; абсолютные пути также поддерживаются. Выходной путь CLI задаётся явно, company_id никогда не используется для конструирования имени файла.

## Нормализация и происхождение

UTF-8/BOM, `;`, CSV data record numbers с 1, включая корректный учёт quoted newline. Дубли заголовков, неверная ширина строк, пустые ID и отсутствующие обязательные колонки отклоняются. Одинаковые строки схлопываются с диагностикой и сохранением всех record numbers; содержательно различающиеся записи блокируются как `profile_conflict`.

Продукты: `1`/`1.0` true, `0`/`0.0` false, остальные значения null. Неожиданные значения дают limitation. Категории возраста/работников/stage/group/client_type сохраняются; SPARK fallback не используется. Revenue — finite nonnegative Decimal, наружу decimal-string; допустима только явно заданная dot-decimal запись без разделителей тысяч/десятичной запятой. Единицы и период неизвестны.

Отрасли — авторские интервалы ОКВЭД, версия и полный hash конфигурации входят в `normalization_version`; 49–53 transport, 58–63 it, 45–47 trade. Невалидный/непокрытый ОКВЭД не угадывается. Geography aliases задаются конфигурацией; CN/TR/RU/KZ — только известные aliases. Неизвестная страна сохраняется `UNKNOWN:<raw token>` с quality unknown; false FTS с непустыми странами помечает countries и fts_present conflicting. Сырой регион/страна воспроизводится через hash/CSV record/column, дополнительных запрещённых schema полей в Origin нет.

Все используемые бизнес-поля имеют Feature с разрешающимся JSON Pointer. Feature ID = `<company_id>:<pointer>`. CSV-origin содержит путь/opaque locator, hash, record numbers, columns; derived origin содержит входные Feature IDs и версию правила. Признаки отрасли и metadata snapshot имеют явные refs. Версия профиля учитывает содержимое, весь source hash, config hash, содержательные даты и граф; техническое `imported_at` исключено. `source_manifest` должен подтвердить hash CSV и timezone-aware даты; без него data_as_of/exported_at=null, historical_applicability=unknown. Техническая дата загрузки не объявляется датой состояния бизнеса.

## Граф

CSV читается потоково, в ephemeral SQLite записываются только строки выбранных компаний; группировка directed pairs выполняется на диске с page cache 2 MiB. Выходной список соседей и provenance по сохранённым рёбрам занимают память, пропорциональную выбранному подграфу, а не полному входному CSV. Повторные запросы используют ограниченный двумя записями cache по source hash/путям/выбранным ID/политике/набору известных профилей; CSV повторно не декодируется. Для проверки версии источник заново хэшируется потоково.

Self-loops исключены отдельно. Положительная finite сумма и положительный целочисленный count обязательны; count `2.0` допустим по явному правилу. Все строки повторяющейся пары исключаются целиком, даже при одной невалидной строке. Ambiguous имеет приоритет над invalid в coverage. Неизвестные профили соседей сохраняются. incoming/outgoing имеют отдельные знаменатели, record/amount fractions и share_quality. При потерях либо глобальных строках с неопределёнными ID качество консервативно limited; неизвестная исключённая сумма не становится нулём. Empty loaded отличается от not_loaded.

Derived graph origins разрешаются в `state_dir/artifacts/<sha256>.json`: hash источника, cleaning policy, номера сохранённых записей по направлениям, знаменатель, coverage, глобальная статистика, hash набора known IDs. Исключённые записи воспроизводятся повторным применением зафиксированной политики к источнику с данным hash. `stats.artifacts` также возвращает эти артефакты. Версия графа учитывает источник/политику/known IDs; attach того же графа идемпотентен и не дописывает суффиксы.

## Проверки

`uv run --frozen python -m unittest ranking.tests.test_profiles ranking.tests.test_graph -v`: **16 tests passed**. Тесты работают на синтетике, tempfile, без raw/network/LLM.

| Кейс | Фактический статус |
|---|---|
| P01 | passed: tri-state и unexpected flag |
| P02 | passed: identical collapse, conflicting rejection |
| P03 | passed: категории, decimal-string, неизвестные units/period |
| P04 | passed: новый opaque ID, unknown OKVED/country, FTS conflict |
| P05 | passed: CompanyProfile schema, каждый pointer/value, origin/refs |
| P06 | passed: 30/70, отдельный incoming, self-loop и duplicate exclusion |
| P07 | passed: неизвестный сосед, loaded-empty, исходный профиль не мутирует |
| P08 | passed: CSV/config/payment/knownness/policy version changes; repeat/reattach stable |
| P09 | passed: 100+100 excluded, C share 0.8 при retained fraction 0.5, limited; invalid duplicate не спасает пару |
| P10 | passed: undated snapshot, imported_at version invariant, подтверждённая дата меняет version |

CLI без банковского датасета проверяется unit test: `profile --synthetic --company-id DEMO_IT --companies-csv ranking/tests/fixtures/profiles/companies.csv --output <tempfile>`. Unknown ID даёт явную ошибку.

Отдельный CLI smoke с графом:

```bash
uv run --frozen python -m ranking profile --company-id DEMO_LOGISTICS --companies-csv ranking/tests/fixtures/profiles/companies.csv --payments-csv ranking/tests/fixtures/profiles/payments-clean.csv --with-payments --synthetic --state-dir data/processed/personalization/fixture-profile-smoke --output data/processed/personalization/fixture-profile-smoke/profile.json
```

Общий discovery первоначально обнаружил ошибку отдельного geography B6 fixture задания 03; передано его владельцу. Оркестратор после исправления сообщил 52 tests passed и успешный demo. Владение результатами общего прогона — итоговый отчёт оркестратора; эта задача самостоятельно подтверждает 16 профильных тестов и CLI.

## Локальная проверка реальных CSV

Raw доступен; synthetic acceptance выполнена первой. Результаты только в ignored `data/processed/personalization/`: `02-real-validation.json`, `real-profiles.json`, `real-profiles-with-graph.json`, `artifacts/`. Реальные профили/платежи не включены в Git.

Полный проход нормализации: **85 525 записей, 85 514 уникальных ID, 85 513 nonconflicting profiles**; U_428697 содержит 12 конфликтующих записей и исключён из выдачи. Hash совпадает с существующим `data/processed/ranking/data_validation.json`; его агрегаты unique/normalized также совпали. Все nonconflicting профили построены, provenance source hash проверен; JSON Schema отдельно проверена для выбранных U_3263/U_44792/U_1941 до и после attach, а не заявлена для всех 85 тысяч объектов.

Платежный файл: **1 380 585 записей**, **1 278** касаются выбранных трёх ID. Первый проход: загрузка + полная нормализация 22.744 s, graph 1.669 s; peak RSS процесса **257 044 KiB** (включает весь индекс компаний, это не отдельная оценка RAM графа). SHA-256 обоих raw до/после совпали. После окончательных дополнений provenance выбранные три профиля и граф повторно сгенерированы и проверены актуальной схемой; raw снова не изменился.

## Ограничения и передача

Период/валюта платежей неизвестны; наблюдаемая доля не равна обороту или зависимости, payer/payee не доказывают покупателя/поставщика. Product flags не раскрывают сумму/ставку/условия кредита и не описывают другие банки. Нет entity mapping из названия новости в ID, нет inferred собственности/должности, NEGATIVEMARK не используется. Historical replay фиксирует версию профиля, но не доказывает его историческую достоверность. Производный артефакт графа требует сохранённого локального source CSV для повторного построения; Git demo остаётся полностью синтетическим.

Следующий агент получает schema-valid envelope, JSON Pointer Feature IDs, три состояния продукта, conflicting/unknown качества стран и graph coverage с обязательным share_quality gate. Сильный share-based сигнал разрешён только clean_observed; limited — только слабый сигнал наличия связи по правилам задания 03.

Зависимость: jsonschema 4.25.1 из общего pyproject/uv.lock задания 01; SQLite/CSV/Decimal — стандартная библиотека. Модели не использованы, **no_llm_calls**, стоимость 0. Live API/UI/LLM quality: **not_run**, за рамками 02. Внешних блокеров завершения 02 нет.
