# Данные компаний и приём событий

Старый эвристический ранкер удалён. Новый отборщик проектируется отдельно в [retriever/](../retriever/README.md).

В этой папке сохранены:

- `b6.py`, `contracts.py`: строгий приём B6 v0.1/v0.2, проверка ссылок и доказательств;
- `event_store.py`: неизменяемые версии событий, hashes и время получения;
- `profiles.py`, `profile_cli.py`: нормализация и загрузка профилей компаний;
- `graph.py`, `entity_mapping.py`: наблюдаемые платёжные связи и явное сопоставление сущностей;
- `audit_data.py`: аудит исходных данных;
- `experiments/cpu_news_embeddings/`: воспроизводимые CPU-эксперименты E5 384/768/1024.

```bash
uv run --frozen python -m ranking --help
uv run --frozen python -m ranking validate-b6 --input docs/b6-examples/v0.2/normal.json
uv run --frozen python -m unittest discover -s ranking/tests -v
```

Команды `rank` и `demo`, `engine.py`, `signals.py`, `rank_cli.py` и конфиги старого скора удалены. Пустой список кандидатов и отсутствие реализованного ретривера — разные состояния; до реализации нового API нет работающего персонализированного отбора.

Схемы `Relevance 0.1` и `ranking-result` пока сохраняются для проверки старых артефактов и изолированных тестов аналитика. Они не являются новым выходом ретривера. [Переходный контракт](CONTRACT.md).
