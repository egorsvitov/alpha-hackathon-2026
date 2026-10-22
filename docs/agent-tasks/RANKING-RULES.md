# Формат начальных правил ранжирования

Спецификация для 01A/03A, не реализованный ранкер. [Восемь полных правил](ranking-rule-examples.json) перенести в runtime-конфиг в 03A; заголовок status убрать, spec_version сохранить. Агент создаёт schema/config validator и тесты на перечисленные ниже случаи. Бизнес-приоритеты и баллы — начальные авторские гипотезы, не экспертная разметка качества.

## Как исполнять правило

1. event.event_type должен входить в event_match.event_types. Внутри **одного claim** хотя бы один selector claim_any_of должен совпасть. Нельзя взять объект из одного claim, модальность из другого и географию из третьего.
2. Selector.path — JSON Pointer относительно claim. Допускаются только `/object` и `/measurements/*/metric`; `*` разрешён только для перебора элементов measurements, refs сохраняют фактический индекс. Объект берётся из структурированного поля, не из подстроки claim.text/title/attribution. При совпадении через measurement проверить, что его metric не спорен. Если object спорен, нельзя обходить конфликт через зависимое измерение.
3. Нормализация alias: Unicode NFKC → casefold → ё/е → trim и схлопывание whitespace. Затем полное равенство строки одному alias. Никаких stem/substring/fuzzy совпадений в этом DSL: `diesel` не совпадает с `diesel_company`. Нужный вариант явно добавить в aliases и проверить тестом. Расширяемые неизвестные метки B6 сохраняются, но сами по себе не включают это правило.
4. company_match — expression: `any_of:[expression,...]`, `all_of:[expression,...]` либо leaf `{path,op,value?}`. Списки непустые. Разрешённые leaf paths: `/products/loan`, `/products/deposit`, `/products/leasing`, `/okved/code`, `/industry`, `/employees_band`, `/fts_present`. Операторы: equals (точное равенство типизированного значения), prefix (строка ОКВЭД), known (значение не null/пустое, value отсутствует). null не равен false. unknown/conflicting quality не даёт подтверждённого совпадения, даже если scalar похож на ожидаемый.
5. Каждый успешный leaf даёт feature_id из профиля. Для any_of сохранить все применимые листья для диагностики, но awards правила начислить один раз. industry=transport/it должен существовать в versioned normalization mapping из 02A; правило не меняет отрасль компании. Если код неизвестен, prefix не проходит. code `24` совпадает с `24`, `24.*`, но не `240`; граница — точка или конец кода. Это же правило относится к `49.41` и вложенным кодам.
6. Применить geography_policy и общую policy модальности. Только затем получить awards. Для каждой award создать reason с rule_id, refs claim/совпавших полей/использованных features и ограничениями; scores вычисляет 03B, а не сам config loader. Незнакомый operator/path/policy/group в конфиге — config error, не молча пропущенное правило.

## География

- `ru_or_unknown`: RU в geographies совпавшего claim допускает канал. Если массив пуст — только условный канал с `event_geography_unknown`. Явно известная география без RU не включает этот локальный канал. Регион регистрации профиля не доказывает территорию закупок/работы; этот недостаток остаётся limitation даже при RU. Пример правила оценивает возможную экспозицию, не долю российских операций.
- `country_overlap`: нужны fts_present=true, непротиворечивые известные countries профиля и пересечение с нормализованными country-кодами geographies **того же claim**. Например CN/CN совпадает, CN/US — нет. Конфликтующий FTS/country-признак или unknown-география не дают 18 points; допустим отдельный metadata candidate без этих баллов. Не считать регистрацию в регионе подтверждённой ВЭД.
- `currency_unconfirmed`: нужны fts_present=true и хотя бы одна непротиворечивая известная страна профиля. Направление курса/валюта контрактов остаются неизвестными. Получается только условный potential_fx_exposure, без country points и утверждения «у компании долларовый долг». Упоминание US как территории публикации не доказывает валюту договора и само по себе не исключает этот FX-канал.

Неизвестные названия стран нельзя превращать в ISO-коды догадкой. Global policy защищает независимые каналы: отсутствие RU в локальном топливном правиле не отменяет отдельно подтверждённый entity/counterparty match.

## Модальность и конфликт

| B6 claim | Результат для этих правил |
|---|---|
| asserted + affirmed | Допустимые awards, но экономический механизм всё равно conditional_mechanism; macro имеет metadata_match |
| forecast/plan/proposal + affirmed | Те же стартовые баллы внимания, conditional_mechanism и явная модальность в тексте; прогноз не объявляется состоявшимся |
| polarity=negated/unknown | Эти awards не начисляются; сохранить диагностический candidate/reason, отдельные entity-каналы проверяются независимо |
| opinion или modality=unknown | Только metadata candidate, без claim-based awards из примеров |
| Спорный selector или обязательный признак применимости | Эти awards заблокированы; другие независимые правила могут работать |
| Спорное числовое value при ясном object/metric | Тематический механизм может работать с limited evidence; числовые бонусы/вычисления запрещены |

stage/action сохраняются в reason. Rate hold не превращается в повышение: для loan текст говорит об условиях финансирования, а не об увеличении расходов. Для steel нельзя по одному ОКВЭД 24/25 сказать, покупает компания сталь или продаёт: причина — отраслевой контекст, не знак финансового эффекта.

Awards объединяются по группам через max/cap из 03, не суммируются за aliases, статьи или одинаковые правила. Пустой набор бизнес-совпадений не повышается за свежесть. Лексический fallback и entity mapping остаются отдельными каналами с их ограничениями из задания 03.

## Минимальные пары для тестов

Все поля, кроме проверяемого, одинаковы; temporal eligibility обеспечивается тестовыми часами. Числа ниже — базовые award points до роли/возраста/cap, не готовый score.

| Правило | Positive | Negative / обязательная оговорка |
|---|---|---|
| policy_rate_loan_v1 | policy_rate, object=policy_rate, loan=true → product 28, macro 4 | loan=null/false → правило не сработало; hold не означает рост долга |
| policy_rate_deposit_v1 | Те же поля, deposit=true → product 20, macro 4 | Наличие слова «депозит» в title без product flag не подходит |
| policy_rate_leasing_v1 | Те же поля, leasing=true → product 15, macro 4 | Три product flags не суммируются: max product=28, macro=4 |
| fuel_transport_v1 | price_change, object=diesel, claim geo=RU, industry=transport → mechanism 25 | IT без подходящего ОКВЭД; diesel только в тексте; geo=US — 0 для этого правила |
| wages_it_v1 | market_indicator, object=wages, industry=it, employees_band известен → mechanism 10 | employees_band=null либо wages только в названии издателя — 0; план найма неизвестен |
| steel_manufacturing_v1 | price_change, object=steel, okved=24.10 → industry 42, mechanism 25 | okved=240 или 62.01 — 0; направление эффекта не установлено |
| restriction_country_v1 | restriction, object=sanctions, claim geo=CN, FTS=true, countries=[CN] → geography 18 | countries=[US], FTS=false/unknown/conflicting, forecast author=CN — 0 |
| fx_fts_v1 | fx_rate, metric=usd_rub, FTS=true, countries=[CN] → mechanism 15 | FTS=false или нет известной страны — 0; валютный риск только потенциальный |

Каждое правило дополнительно проверить с forecast, negated, перестановкой/дублированием claims, неизвестным alias и конфликтом selector/value. Положительные refs должны разрешаться в тот claim и профиль, которые действительно прошли условия. Для 03A достаточно проверить awards/candidates; итоговая формула и сортировка тестируются в 03B.
