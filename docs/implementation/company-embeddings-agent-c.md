# Итог зоны C

Отчёт составлен оркестратором. Назначенный gpt-6.1-sol / medium остановился из-за лимита Codex; это не отчёт об успешной самостоятельной поставке агента. Сохранённые частичные результаты проверены и доведены оркестратором.

Query snapshot adapter, exact dense, evaluation dataset/metrics/runner/report и матрица 7 вариантов. Подготовлена форма 800 пар без меток; качество not_evaluated. BM25/structured/fusion/service и semantic batch validator интегрированы оркестратором. Графовый static_context — диагностический; strict не использует граф без available_at. Разбиение на human-reviewed dev/test и интеграция аналитика остаются отдельными зависимостями.

Команды проверки, версии, фактические измерения и границы готовности: [общий отчёт](company-embeddings-report.md) и [машинный статус](company-embeddings-status.json).
