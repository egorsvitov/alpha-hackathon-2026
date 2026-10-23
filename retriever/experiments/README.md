# Гипотезы интересов по ОКВЭД

Опциональный эксперимент в ветке `feature/impact-retriever`. Каталог содержит
сгенерированные агентами предположения о темах новостей для каждого ОКВЭД из
исходного CSV. Это не новые факты о компании и не подтверждение её расходов,
продуктов, контрагентов или технологического стека.

Два новых представления:

- `activity_topics`: возможные отраслевые темы;
- `activity_finance`: возможные драйверы затрат, цен, спроса и оборотных средств.

Условные темы (`conditional_topics`) содержат необходимые условия применимости.
Они сохраняются в каталоге, но **не включаются в эмбеддинг автоматически**.
Существующий `finance` по подтверждённым банковским продуктам не изменён.
Компании с одинаковым ОКВЭД переиспользуют два вектора; регион, банковские
продукты и платёжное окружение в новые векторы не входят.

## Что создано локально

Корень: `data/processed/retriever/activity-hypotheses-v1/` (игнорируется Git).

| Путь | Содержимое |
|---|---|
| `inputs/activities.json`, `inputs/shard-*.json` | 546 исходных кодов и задания трём агентам |
| `generated/shard-*.json` | Описания от GPT-6 Luna, по 182 кода |
| `generated/review-*.json` | Замечания взаимной проверки и предложенные исправления |
| `catalogue/catalogue.json` | Проверенный по структуре и покрытию каталог |
| `catalogue/company-activity-map.json` | 85 513 company_id → ОКВЭД |
| `catalogue/manifest.json` | Происхождение, hashes, исключения и повторения текстов |
| `catalogue-review.md` | Все описания для чтения и ручного ревью |
| `gpu-index/vectors.npy` | 1092 × 384, float32, L2-нормированные векторы |
| `gpu-index/metadata.json` | Тексты запросов, позиции, каталог, модель и связи с компаниями |
| `gpu-index-build-report.json`, `validation.json` | Замеры и проверки |
| `comparison/README.md`, `comparison/U_*.json` | Прогон на прежних 8 компаниях / 100 новостях |
| `comparison/events/` | Новости, пересчитанные тем же GPU-энкодером |

Каталог не проходил экспертную разметку; `human_review=pending`. Противоречивые
строки одной компании исключаются, идентичные дубли сворачиваются. В текущем
источнике исключена одна неоднозначная компания. Коды сравниваются как строки:
например, `41.2` и `41.20` остаются отдельными исходными записями.

## Воспроизведение

Из корня репозитория. Все выходные каталоги сборки должны быть новыми.

```bash
PYTHONPATH=. .venv/bin/python scripts/build_activity_hypotheses.py prepare \
  --source data/companies/df_Uid_data_hack.csv \
  --output data/processed/retriever/activity-hypotheses-next/inputs
```

Генерация — работа трёх Codex-агентов `gpt-6-luna`, не скрытый платный API-вызов
из скрипта. Агент получает свой shard и возвращает JSON-массив с полями
`okved_code`, `activity`, `search_description`, `financial_search_description`,
`conditional_topics: [{topic, condition}]`, `status: hypothesis`,
`generator_model: gpt-6-luna`. Нужны короткие законченные описания, конкретные
для товара/услуги данного кода. Нельзя перечислять соседние нерелевантные
отрасли, выдумывать операции компании или использовать тестовые новости для
составления описаний. После генерации выполняется взаимное ревью.
Автоматический валидатор проверяет структуру и покрытие, но не заменяет
смысловую проверку. Модели не обучались и не дообучались.

```bash
PYTHONPATH=. .venv/bin/python scripts/build_activity_hypotheses.py compile \
  --source data/companies/df_Uid_data_hack.csv \
  --shards data/processed/retriever/activity-hypotheses-v1/generated/shard-{1,2,3}.json \
  --output data/processed/retriever/activity-hypotheses-next/catalogue
```

GPU: E5-small, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`,
PyTorch FP32, masked mean pooling + L2, `query:` для компании и `passage:` для
новости. Лимит 512 токенов с префиксом: превышение вызывает ошибку, усечения нет.
Модель загружается только из локального pinned snapshot, `trust_remote_code=False`,
только safetensors. Для получения snapshot используется официальный Hugging Face
репозиторий [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
и указанная revision; нужны `config.json`, `model.safetensors`, `tokenizer.json`,
`tokenizer_config.json`, `special_tokens_map.json`, `README.md`.

В выполненном прогоне использовано готовое Jupyter-окружение **Python (Study GPU)**:
`../mt-2026/research/mostransport-hackathon-research/.venv`, Python 3.13,
PyTorch 2.14.0+cu130. Исходное окружение не изменялось. В
`.cache/activity-gpu-runtime/` создана отдельная venv того же Python, её
`site-packages/study_gpu.pth` подключает существующие site-packages PyTorch;
Transformers и дополнительные зависимости установлены только в новую venv.
`requirements-gpu.lock` фиксирует фактически использованный dependency closure.
Пересоздание всех CUDA-зависимостей на другой машине не проверялось; текущий
прогон выполнен с установленным ранее PyTorch. Для другого GPU-окружения нужно
пересобрать оба индекса: fingerprint включает версии runtime и параметры GPU.

```bash
PYTHONPATH=. .cache/activity-gpu-runtime/bin/python scripts/build_activity_hypotheses.py encode \
  --catalogue data/processed/retriever/activity-hypotheses-v1/catalogue \
  --output data/processed/retriever/activity-hypotheses-next/gpu-index \
  --model-path /home/pavel/.cache/huggingface/hub/models--intfloat--multilingual-e5-small/snapshots/614241f622f53c4eeff9890bdc4f31cfecc418b3

PYTHONPATH=. .cache/activity-gpu-runtime/bin/python scripts/review_activity_hypotheses.py \
  --activity-index data/processed/retriever/activity-hypotheses-v1/gpu-index \
  --source-event-index data/processed/retriever/company-embeddings/event-index \
  --showcase data/processed/retriever/showcase-8-companies-20261022 \
  --model-path /home/pavel/.cache/huggingface/hub/models--intfloat--multilingual-e5-small/snapshots/614241f622f53c4eeff9890bdc4f31cfecc418b3 \
  --output data/processed/retriever/activity-hypotheses-next/comparison
```

## Использование готовых векторов без GPU

```python
from retriever.experiments.activity_index import ActivityIndex
from retriever.index import EventIndex

root = 'data/processed/retriever/activity-hypotheses-v1'
companies = ActivityIndex.open(root + '/gpu-index')
events = EventIndex.open(root + '/comparison/events')
hits = companies.search_company('U_100587', events, top_k=20)
# Каждый hit: event_id, title, cosine score, match {view_kind, okved_code,
# text, status='hypothesis', chunk_ref со ссылкой на версию события}.
```

Поиск по готовым векторам работает на CPU через NumPy. Старый INT8-индекс
с этими векторами не смешивается: несовпадение fingerprint вызывает ошибку.
Код `RetrievalService`, его H3 fusion и упаковка в бюджет аналитика не менялись.
Новый экспериментальный поисковый интерфейс не является заменой
`ImpactCandidateBatch` и не добавляет гипотезы в `verified_facts`.

Сравнение использует одинаковый GPU-энкодер для старых текстов запросов и новых
гипотез, максимум cosine по представлениям/чанкам и весь корпус из 100 событий.
Это диагностический поиск без порога релевантности, бюджета LLM и калибровки.
Изменение позиций не равно росту Recall@20; нужна независимая разметка.
