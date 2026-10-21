# News pipeline — сбор внешних новостей (пункт 2)

Универсальный парсер, который собирает поток внешних новостей в единый
промежуточный формат для NLP-модуля (пункт 1). Выход — JSONL-накопитель,
где каждая строка — одна статья в формате команды.

## Зачем это

Пункт 1 (агент) превращает статьи в `EventBundle` по
`docs/contracts/event-bundle.schema.json` и строит персонализированный вывод.
Наша задача — дать ему чистый, предсказуемый поток сырых статей без LLM:
дёшево, быстро и легко проверять. LLM к парсеру не подключается.

## Формат записи (согласован с командой)

```json
{
  "article_id": "cbr_rss:3f2a9c1d0e4b",
  "source": "cbr_rss",
  "url": "https://www.cbr.ru/...",
  "title": "Заголовок",
  "published_at": "2026-10-20T12:00:00+03:00",
  "language": "ru",
  "text": "Текст для обработки NLP-модулем..."
}
```

- `article_id` — стабильный: `source:<sha1(url)[:12]>`. Повторный проход по той же
  ссылке даёт тот же ID, это основа дедупликации.
- `published_at` — ISO-8601 или `null`. Дата не выдумывается.
- `text` — может быть пустым, если текст не извлёкся (статья всё равно сохраняется).

## Запуск

```bash
# установка зависимостей (Python 3.10+)
python3 -m venv .venv_news
.venv_news/bin/pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r news/requirements.txt

# все источники
.venv_news/bin/python news/collect.py

# только макро/официальные источники
.venv_news/bin/python news/collect.py --category macro official

# без полного текста (быстрая проверка объёма)
.venv_news/bin/python news/collect.py --headlines-only

# smoke-тест без сети
.venv_news/bin/python news/smoke_test.py
```

Результат — `data/processed/news/news_corpus.jsonl` (накопитель, переживает
перезапуски). Скачанные страницы кэшируются в `data/processed/news_cache/`.

Замечание: на корпоративной сети PyPI закрыт SSL-проверкой, поэтому в примерах
используются `--trusted-host`. Версии зависимостей зафиксированы в
`news/requirements.lock`.

## Структура

```text
news/
  collect.py           # CLI-точка входа
  smoke_test.py        # офлайн-проверка пайплайна
  requirements.txt     # зависимости
  requirements.lock    # зафиксированные версии
  sources.json         # реестр источников (единственное место их настройки)
  core/
    model.py           # Article / ArticleCandidate (контракт входа)
    registry.py        # SourceSpec + реестр
    fetcher.py         # HTTP: ленты, текст, кэш (единственный сетевой слой)
    identity.py        # стабильные ID + нормализация времени
    store.py           # JSONL-накопитель (атомарная запись)
    pipeline.py        # оркестратор прохода
```

## Источники и лицензии

В `sources.json` — начальный пул деловых/официальных лент (ЦБ РФ, Минфин,
РБК, Интерфакс, Коммерсантъ, Ведомости, Lenta). Пул можно расширять без
изменения кода: `kind` = `rss` / `json` / `html`, `category` — группа для
фильтрации. Перед добавлением источника проверяйте условия использования
его ленты: указываем название, не перепродаём контент, соблюдаем лимиты.

## Чего здесь нет

- NLP, классификации и извлечения фактов — это пункт 1 (агент), выход `EventBundle`.
- Ранжирования под компанию — модуль `ranking/`.
- Позиции банка / аналитики — RAG в `llm/rag/`.