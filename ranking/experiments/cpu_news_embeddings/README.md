# Эксперимент: 100 новостей и CPU-эмбеддинги

Изолированный эксперимент; production-ранкер и корневые зависимости не меняет. Все команды ниже выполняются из корня репозитория.

## Что проверяется

1. Сбор 100 уникальных реальных новостей из публичных RSS/страниц.
2. Представление каждой записи как валидного B6 v0.2 с точными цитатами.
3. Получение 384-мерных нормированных эмбеддингов на CPU.
4. Скорость одиночной и пакетной обработки, включая токенизацию и pooling.
5. Поиск ближайших компаний в явно ограниченной выборке 256 профилей.

**B6 здесь — quote-only упаковка, а не работа LLM-препроцессора.** Lead сохраняется как исходный текст, modality/polarity неизвестны, semantic fields пусты, quality=partial. Семантическое извлечение фактов, качество деления на события и оценки бизнес-влияния не измеряются. Одна статья используется как одна экспериментальная запись; разные статьи об одной истории не объединяются.

## Запуск

```bash
uv sync --project ranking/experiments/cpu_news_embeddings --python 3.12 --frozen
uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/collect.py
uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/encoder.py
uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/benchmark.py --cpus 2 --repeats 3
uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/verify.py
uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/make_report.py
```

Первые два сетевых этапа нужны один раз. Для повторного замера запускать только benchmark на сохранённом корпусе. Повторный collect обновляет набор новостей; сравнивать скорости на разных корпусах без учёта длин нельзя.

Пример использования encoder из Python в этом окружении:

```python
from ranking.experiments.cpu_news_embeddings.encoder import Encoder

encoder = Encoder(threads=2, max_tokens=512)  # веса заранее скачаны encoder.py
vectors = encoder.encode(["Аэропорт изменил порядок обслуживания рейсов."])
assert vectors.shape == (1, 384)
```

`--cpus` ограничивает affinity процесса и существующих helper threads в Linux. ONNX intra-op workers — максимум 2, inter-op — 1. Доступен только CPUExecutionProvider; PyTorch/CUDA не устанавливаются. Значения времени специфичны для текущего CPU, его инструкций, фоновой нагрузки и корпуса, а не гарантированы для любого VPS.

## Модель и тексты

- Модель: `intfloat/multilingual-e5-small`, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`.
- Официальный ONNX-экспорт `onnx/model_qint8_avx512_vnni.onnx`, динамическая INT8-квантизация, 118 346 824 байта.
- Токенизатор из того же commit; mean pooling с attention mask, затем L2-нормализация.
- По рекомендации model card: новость как retrieval query с `query: `, описание компании как `passage: `.
- Основные режимы: title+lead до 256 токенов; title+доступный текст до 512.
- Дополнительный stress: ОДНА самая длинная реальная статья повторно кодируется 100 раз без кэша результата, чтобы проверить длинные входы. Это отдельный вычислительный тест, не ещё 100 разных новостей.

Источники: [model card](https://huggingface.co/intfloat/multilingual-e5-small), [проверенный commit](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3).

## Сбор и происхождение

Источники — RSS Интерфакса, Ленты и Коммерсанта. При запрете загрузки HTML в robots.txt страницы не скачиваются: используется доступный RSS-анонс. Такие записи отдельно отмечены `extraction=rss_summary`, а извлечённые статьи — `trafilatura_public_html`. Полнота оригинала не заявляется.

В каждой записи сохранены URL, publisher, published_at, retrieved_at, article_id и hash текста. Проверяются 100 уникальных URL, 100 различных текстовых hashes, JSON Schema/semantic checks B6 и точные срезы evidence по сохранённому тексту. Локальный корпус и модель находятся в игнорируемой `artifacts/`, в Git не публикуются.

## Артефакты

| Файл в artifacts/ | Содержание |
|---|---|
| articles.jsonl | 100 новостей с доступными текстами и происхождением |
| events.v0.2.json / events.v0.2.jsonl | Тот же корпус в B6 v0.2 |
| collection.json | Источники, ошибки/пропуски, типы извлечения, hashes файлов |
| model/manifest.json | Модель, точная revision и hashes скачанных файлов |
| benchmark.json | Полные замеры, окружение, токены, truncation, RSS и проверка batch consistency |
| news.embeddings.npy | Матрица 100×384 float32 |
| companies.sample.json | Выборка реальных локальных компаний и текстовые проекции |
| companies.sample.embeddings.npy | Матрица профилей |
| search.examples.json | Примеры ближайших компаний; НЕ разметка релевантности |

Подготовка компании — лёгкая экспериментальная проекция CSV: деятельность, ОКВЭД, регион регистрации, продукты, страны ФТС. Это не полный CompanyView с группой/графом. Выбирается не более одного представителя каждого ОКВЭД, затем 256 записей с seed=42. Выручка не используется. Полная база 85 тысяч компаний не векторизована этим экспериментом.

## Как читать результаты

- Throughput — число обработанных текстов / полное время encode, усреднённое по трём проходам.
- В encode включены токенизация, ONNX inference, pooling и normalization. Сбор, загрузка модели, warmup и подготовка компаний измеряются отдельно.
- Для batch_size=1 p95 — задержка одного текста. Для batch_size=8 p95 — задержка батча; amortized ms/news не является задержкой пользователя.
- На коротких входах батч может быть медленнее: padding выравнивает все тексты до самого длинного в батче. Это измеряется, а не скрывается.
- Корпус содержит RSS-анонсы и несколько более длинных статей. Долю обрезанных входов и распределение токенов смотреть в benchmark.json.
- Высокая cosine similarity не доказывает бизнес-влияние. Recall/precision рекомендаций и полнота аналитики без независимых меток не оценены.

Фактический итог последнего запуска: [RESULTS.md](RESULTS.md).

## Сравнение с 768 и 1024 измерениями

Добавлен одинаковый тест для `intfloat/multilingual-e5-base` (768) и `intfloat/multilingual-e5-large` (1024). Веса официальные ONNX INT8, revisions зафиксированы в `compare_dimensions.py`. Small (384) также запускается заново на том же корпусе. Каждый размер проверяется отдельным процессом с двумя одинаковыми логическими CPU; это позволяет отдельно измерить peak RSS.

```bash
uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/compare_dimensions.py
uv run --project ranking/experiments/cpu_news_embeddings --frozen python ranking/experiments/cpu_news_embeddings/report_dimensions.py
```

Итог: [DIMENSIONS_RESULTS.md](DIMENSIONS_RESULTS.md). Подробные результаты каждого размера — `artifacts/comparison-384/`, `comparison-768/`, `comparison-1024/`. Исходный `artifacts/benchmark.json` не перезаписывается. Полнота входов, токенизация, prefix, pooling и нормализация одинаковы по методике; число слоёв и весов моделей различается. Это сравнение моделей, а не чистый эксперимент с изменением только размерности. Сходство результатов batch=1 и batch=8 проверяется порогом minimum cosine >0.95; неуспех этого диагностического порога сохраняется как предупреждение в отчёте, не скрывается и не объявляется успешной проверкой качества.
