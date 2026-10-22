# Миграция потребителя на B6 v0.2

Выполнено локально 22.10.2026. Поддержка v0.1 сохранена. Commit/push в этой задаче не выполнялись.

## Что изменено

- `ranking/contracts.py`: при проверке EventBundle схема выбирается по schema_version. Поддерживаются 0.1/0.2, неизвестная версия отклоняется явно. Это действует также на существующий CLI RAG.
- `ranking/b6.py`: поддерживаются отдельный JSON-объект, JSON-массив объектов и JSONL. Сохранились ограничения размера, запрет повторных JSON-ключей/NaN, проверка refs, цитат и evidence coverage. Дополнены проверки active claims и согласованности типа relations, которые v0.2 оставляет коду.
- `ranking/signals.py`, `ranking/engine.py`: scenario сохраняется в основаниях и ограничениях. Сценарные claims не используются как доказательство уже действующего ограничения для durable boost; before/after/comparison не превращаются в автоматический текущий денежный эффект.
- Контекст аналитика принимает обе версии через общий валидатор и передаёт исходные claims целиком, включая scenario/value_role. Новая схема не конвертируется в старую.
- `llm/agent/qa.py`: сценарный AI-вывод без условий блокируется. Численная AI-интерпретация before/after/comparison без смысловой проверки также блокируется; точный пересказ исходного source_fact допускается. Это консервативное ограничение fixture-аналитика, не полноценное понимание финансовой арифметики.
- Обновлены контракт/README и интеграционная проверка producer→consumer: она теперь ожидает успешный приём обеих версий.

EventStore сохраняет полный исходный payload; схема SQLite не менялась. Повтор того же payload даёт unchanged, разное содержимое при прежнем `(event_id,event_version)` по-прежнему даёт EventVersionCollision. Никакого молчаливого перезаписывания старой базы нет. Для проверки нового producer создана отдельная база в `data/processed/personalization/b6-v02-migration/`.

Выходные версии Relevance v0.1 и Insight v0.2 не менялись: это самостоятельные контракты.

## Файл Домодедово

Отредактирован исходный пользовательский файл:

`/home/pavel/Загрузки/Telegram Desktop/output-b6.json`

В четырёх timestamp-полях — event.first_published_at и sources[0].published_at в обоих событиях — добавлены секунды: `2026-10-21T22:15:00+03:00`. updated_at с дробными секундами не изменён. Остальные значения и массив из двух событий сохранены.

Оригинал сохранён рядом: `output-b6.before-seconds.json`. Полный текст реальной новости в Git не добавлялся.

Проверки этого файла:

1. `ranking validate-b6`: оба bundle valid, schema/semantic errors=0.
2. Штатный CLI `ranking ingest --mode live`: два inserted в отдельной базе.
3. Повтор CLI: два unchanged.
4. Replay-прогон через настоящий EventStore, без конструирования snapshots в обход проверки: два inserted, повтор — два unchanged.
5. На 14 профилях повторена проверка ранжирования: все 28 пар deferred, feed пуст. Причина — отсутствие candidate signals для операционных авиационных событий. Ошибок формата и импорта больше нет; это отдельный нерешённый вопрос рекомендательного ядра.

Остаётся честное warning `original_text_not_checked`: входной bundle содержит цитаты, но не полный архивный текст для повторного сравнения consumer с оригиналом. Это не ошибка формата и не заявляется независимым фактчекингом.

## Тестирование

| Набор | Методов | Результат |
|---|---:|---|
| ranking | 53 | OK |
| RAG | 17 | OK |
| agent | 45 | OK |
| llm integration, включая 11 новых migration tests | 21 | OK |
| upstream events | 194 | OK |
| Всего | 330 | OK |

Новые тесты: все 11 старых/новых примеров, JSON-массив/JSONL, неизвестная версия, строгие timestamps, scenario/evidence coverage, роли измерений, сохранение и повторное открытие базы, коллизия со старым payload, withdrawn без возврата к active, сценарный rank→контекст→fixture analyst→child question, QA и запрет durable inference из альтернативного сценария.

Во время полного upstream-прогона выявлена зависимость одного прежнего теста `test_missing_usage_holds_reservation_after_restart` от текущей даты: после restart он открывал EventStore без своего clock, и сумма попадала в другой московский бюджетный день. В тест добавлена передача существующих тестовых часов. Production budget logic не менялась. После исправления все 194 метода прошли.

Команды:

```bash
uv run --frozen python -m unittest discover -s ranking/tests -q
uv run --frozen python -m unittest discover -s llm/rag/tests -q
uv run --frozen python -m unittest discover -s llm/agent/tests -q
uv run --frozen python -m unittest discover -s llm/tests -q
uv run --no-project --with-requirements llm/events/requirements.lock --with rfc3339-validator==0.1.4 --with rfc3986-validator==0.1.1 python -m unittest discover -s llm/events/tests -q
uv run --frozen python -m ranking validate-b6 --input '/home/pavel/Загрузки/Telegram Desktop/output-b6.json'
uv run --frozen python -m scripts.check_airport_example --input '/home/pavel/Загрузки/Telegram Desktop/output-b6.json' --output data/processed/personalization/airport-example-v02-migrated.json
```

RFC-валидаторы явно включены в изолированный upstream-прогон, чтобы проверка format не зависела от их случайного присутствия в окружении. Зависимости корневого проекта уже содержали их; lock-файлы не изменялись.

Live LLM и платные API не вызывались. Проверка аналитика относится к существующему fixture workflow; подключение настоящей LLM, расширение отбора компаний и backend-интеграция не входят в эту миграцию.
