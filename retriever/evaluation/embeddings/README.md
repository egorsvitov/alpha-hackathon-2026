# Проверка представлений компаний

`dataset.py` проверяет hashes файлов, ссылки и отсутствие пересечения story groups между split. `runner.py` вызывает production `RetrievalService`, включая общий fusion и бюджет. `metrics.py` считает Recall@20, P@20, precision_returned, nDCG@10 и class3 recall; null/weak не превращаются в отрицательную human-метку. `report.py` сравнивает сопоставимые конфигурации, latency и paired company bootstrap после появления разметки.

Текущая форма: 8 компаний × 100 событий, 800 **неразмеченных** пар. Локальные пути:

- `data/processed/retriever/company-embeddings/evaluation/profiles.json` — факты компании;
- `events.json` — исходные B6;
- `payment_contexts.json` — полный подтверждённый контекст рёбер/coverage для одинаковых условий разметки;
- `judgments.json` — форма, все label=null;
- `manifest.json` — hashes и frozen context snapshot.

Аннотатору давать эти файлы, а не результаты/score разных вариантов. `company_context_hash` в строке — hash исходного профиля; дополнительно полный платёжный контекст фиксируется hash файла в dataset manifest и `context_snapshot_id`. Изменение любого из них делает набор другим. Ссылки на соседей допустимы только через имеющиеся рёбра и scoped feature refs.

В текущем корпусе ID событий соответствуют статьям, это не проверенные уникальные новостные истории. Не сделаны production дедупликация и разбиение на проверенные dev/test: split называется `diagnostic`. Прежде чем заявлять качество, человек должен сгруппировать истории для split, разметить пары и закрепить версии; статус `human_reviewed_frozen` выставляется только после этого. Маленький набор остаётся exploratory. Автоматический graph promotion отключён.

Пример диагностической матрицы (7 вариантов, CPU):

```bash
.venv/bin/python -m retriever.evaluation.embeddings \
  --dataset data/processed/retriever/company-embeddings/evaluation/manifest.json \
  --index data/processed/retriever/company-embeddings/event-index \
  --snapshot data/processed/retriever/company-embeddings/graph-sqrt_share \
  --cache data/processed/retriever/company-embeddings/objects.sqlite \
  --temporal-mode static_context \
  --output data/processed/retriever/company-embeddings/evaluation/static-comparison.json
```

Матрица: D1/D3/H1/H3/H3-I/H3-O/H3-IO. В H1/H3 один и тот же набор текстов для BM25 и одинаковые structured inputs. Incoming/outgoing не увеличивают общий вес dense. Payment context для аналитика одинаков для всех вариантов. Graph-specific ссылки в match_context тоже стоят токенов и могут уменьшать число поместившихся событий — это видимая стоимость данного представления.

Чтобы сравнивать другую weight policy, нужен отдельный frozen context snapshot/manifest с теми же фактическими данными; текущий manifest закреплён за sqrt_share. Equal/share/sqrt арифметически проверены и построены отдельно. Выбор policy по качеству без человеческих labels не делался.

Strict-режим на нынешнем графе будет исключать graph views: `available_at` не дан, агрегат относится к периоду до 31 октября. `static_context` не доказывает способность прогнозировать прошлое.

Текущий итог: **quality_status=not_evaluated**. Работающий pipeline и быстрый dot product не заменяют оценку релевантности.
