# Оркестрация 04–05 после проверки LLM-отзыва

Проверен [внешний отзыв](../agent-tasks/review-04-05.md), уточнены задания и общие интерфейсы. Численные оценки из отзыва не использовались. Уже решённое владение Insight v0.2 не переносилось повторно; текущий EvidenceBundle API и enum-статусы сохранены.

## Работа агентов и завершение

Три параллельных агента `gpt-6.1-sol`, reasoning medium:

| Агент | Область | Результат |
|---|---|---|
| bank_rag | Corpus/поиск/даты/admission/EvidenceBundle | [04-result.md](04-result.md) |
| analyst | schemas/контекст/registry/generator/workflow/CLI | [05-result.md](05-result.md) |
| qa_history | QA/immutable store/child questions | [05c-result.md](05c-result.md) |

Агенты остановились по общему лимиту Codex, оставив код и промежуточные результаты на диске. Оркестратор продолжил самостоятельно: исправил невалидный synthetic partial test, проверил реальный проход по корпусу, усилил проверку provenance между RAG/аналитиком и завершил недостающую документацию. Никакие их статусы не использованы как доказательство успешных тестов: ниже фактически повторённые проверки.

## Итоговый маршрут

**EventStore → ранкер → RAG → bounded context → fixture draft → QA → Insight v0.2 → RunStore → уточняющий child run.**

Runtime mode=fixture, Insight/ответ limited, semantic review not_run. Ключи не читались, платных вызовов LLM не было. Используется собственная Python-логика, без готового мультиагентного runtime приложения. Агенты разработки не включаются в продукт как разрешённые модели.

Финальная проверка состоит из четырёх независимых наборов:

```bash
uv run --frozen python -m unittest discover -s ranking/tests -q
uv run --frozen python -m unittest discover -s llm/rag/tests -q
uv run --frozen python -m unittest discover -s llm/agent/tests -q
uv run --frozen python -m unittest discover -s llm/tests -q
```

Фактический итоговый прогон: 53 + 17 + 45 + 10 = **125 тестов, все passed**, включая root cross-module проверки. Время отдельных наборов: 0.923 / 0.169 / 0.246 / 0.407 с. Это test-method count, не число независимо размеченных бизнес-примеров и не метрика качества LLM.

Дополнительная итоговая проверка: Insight v0.2 schema и сохранённый synthetic пример валидны; 59 локальных ссылок и пути implementation reports существуют; raw PDF/OCR и результат демо игнорируются Git; `git diff --check` прошёл. Внешняя B6 schema/исходные fixtures и Insight v0.1 не менялись.

## Проверка с настоящим локальным корпусом

Запущены CLI analyze и затем ask отдельным процессом с `--corpus data/processed/rag_chunks.jsonl`. Новость/компания остаются synthetic, аналитические PDF — реальные предоставленные материалы.

- RAG: partial, 5 кандидатов, каждый pending_review; bank_view claims=0.
- Insight: limited/fixture, semantic_status=not_run; ответ: limited, parent ID совпал с созданной карточкой.
- usage: input/output tokens=0, cost_rub=0, pricing_status=no_llm_calls.
- Результаты/SQLite/артефакты — только ignored `data/processed/personalization/orchestration-04-05/`: `insight.json`, `answer.json`, `summary.json`.

Это подтверждает работающий маршрут и сохранение состояния, не качество финансового анализа. Corpus проверил происхождение 109 чанков по доступным PDF/OCR; визуальная проверка смысла цитат не выполнялась. Strict gold/steel могут давать пустой поиск из-за неизвестной даты/историчности — ограничения не ослаблялись ради демо.

Новые third-party зависимости, модели или датасеты не скачивались. Использованы прежние JSON Schema зависимости/stdlib, предоставленные PDF и отдельно маркированные synthetic fixtures. Backend/frontend не изменялись. Все изменения локальны, без commit/push. Бюджетный шлюз live, кэш/service adapter и независимая оценка остаются заданиями 06–07.
