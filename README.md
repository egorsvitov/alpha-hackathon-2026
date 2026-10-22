# Новостная аналитика для бизнеса

> ML-обновление: старый эвристический ранкер удалён. Новый [Impact Retriever](retriever/README.md) реализует локальный CPU-поиск кандидатов; качество требует ручной разметки, интеграция с аналитиком/backend ещё отдельная задача. Профили, B6, EventStore, граф и CPU-эксперименты сохранены. Исторические инструкции rank/demo ниже или в старых отчётах больше не действуют.

Основные папки проекта: `backend/`, `frontend/`, `ranking/`, `data/` и `llm/`.

```text
backend/                   # FastAPI-сервис (API для фронтенда и интеграции)
    app/
        api/v1/            # Эндпоинты (компании, лента, карточка анализа, чат)
        core/              # Конфигурация и подключение к SQLite
        models/            # SQLAlchemy модели
        schemas/            # Pydantic-схемы (контракт данных)
    main.py                # Точка входа API
    requirements.txt       # Зависимости бэкенда
frontend/                  # Прототип интерфейса (Vite, React, TypeScript), запуск: frontend/README.md
ranking/                   # Профили, B6, EventStore, граф, эксперименты
retriever/                 # Эмбеддинги компаний, платёжный контекст и поиск событий
    audit_data.py          # Готовая заготовка аудита CSV
llm/
    events/                # B1–B5: утверждения, архив и каталог событий
    rag/                   # Подготовка PDF и поиск доказательств
    agent/                 # Интерпретация и самопроверка
        contracts/         # Проект формата ответа и ручной пример
        model_policy.json  # Проект выбора моделей и бюджетной политики
data/
    companies/             # Три исходных CSV
    analytics/             # Девять аналитических PDF
    task/                  # PDF задания
    context/               # HTML-переписка по кейсу
    examples/              # Явно синтетические тестовые данные
    processed/             # OCR, чанки, каталог, результаты аудита и проверок
licenses/                  # Тексты лицензий сторонних компонентов
```

В новых модулях пока нет полноценного приложения. Есть аудит данных, локальный OCR, поисковый baseline и проект контрактов. Исходные материалы перенесены без изменения содержимого.

## Как разделена LLM-часть

- [llm/events/](llm/events/README.md): контракты, эталонная разметка, SQLite-архив, извлечение и нормализация; B1–B5 реализованы, экспорт EventBundle остаётся B6. Реальные вызовы закрыты до задания тарифов и квоты NLP.
- [llm/rag/](llm/rag/README.md): найти подходящие фрагменты аналитики и сохранить связь с документом, страницей и датой.
- [llm/agent/](llm/agent/README.md): использовать событие, профиль и найденные доказательства для объяснения; проверить ответ и при необходимости повторить поиск.

Это модули одной LLM-части; отдельные серверы для них не требуются.

## Что уже можно запустить

Из корня проекта, Python 3.13.5; внешние Python-библиотеки не требуются:

```bash
python3 ranking/audit_data.py
python3 llm/rag/search_rag.py "инфляция бензин август" --topic inflation --top-k 3
python3 llm/rag/verify_artifacts.py
```

Для подготовки корпуса с нуля:

```bash
python3 llm/rag/prepare_rag.py
```

OCR требует Poppler (`pdftotext`, `pdftoppm`) и Tesseract с языками `rus+eng`. Проверенные версии: Poppler 25.03.0, Tesseract 5.5.0, Leptonica 1.84.1. Расположение Tesseract можно задать через `TESSERACT_BIN`, языковых данных — через `TESSDATA_PREFIX`. Сохраненные OCR-страницы используются повторно по хэшу PDF.

Все команды используют пути относительно корня проекта. Исходные данные и полные OCR-копии остаются локальными согласно `.gitignore`; содержимое и подготовка описаны в [data/README.md](data/README.md). Участникам нужно получить исходные файлы из материалов хакатона и положить в указанные папки.

## Ограничения заготовок

В корпусе 109 черновых чанков. OCR-цифры, таблицы и графики не прошли сплошную визуальную проверку. Текущий поиск — BM25 с фильтрами, без генерации и полноценной проверки смысла. Более старый прогноз может оказаться выше нового: окончательный выбор требует учета дат и горизонтов.

Пять поисковых smoke-проверок подтверждают работоспособность на отдельных примерах, а не общую точность RAG. JSON Schema — проект; проверены ссылки ручного примера, отдельный валидатор Draft 2020-12 не запускался. LLM API, агент, бюджетный шлюз, UI и backend пока не реализованы.

Предыдущая оговорка о JSON Schema относится к схеме ответа агента. Для событий B1 уже реализованы модели, B6 v0.2, проверка Draft 2020-12 и точных цитат. Запуск без LLM: установите `llm/events/requirements.lock`, затем `python -m llm.events.check_contracts` и `python -m unittest discover -s llm/events/tests -v` в подготовленном окружении. Подробности и локальная разметка 15 статей — [README B1](llm/events/README.md).

B2 принимает батчи независимо от backend: `python -m llm.events.storage_cli import-batch <articles.json>`. Состояние хранится в игнорируемом `data/processed/events/state.sqlite3`; `list-batches --status importing` и `resume-batch <batch_id>` восстанавливают незавершённый импорт. Версии, постановка заданий и API для B3 описаны в [документации хранилища](llm/events/STORAGE.md).

B3 добавляет `extraction_cli` и последовательный исполнитель с одним запросом на попытку, учётом reasoning-токенов и резервированием бюджета. Офлайн-пример, контракт `b3.1` для B4 и явная миграция SQLite v1 → v2 описаны в [EXTRACTION.md](llm/events/EXTRACTION.md). Успех B3 означает структурно корректные кандидаты, а не проверенные факты.

B4 запускается через `python -m llm.events.normalization_cli normalize --artifact-id <b3_artifact_id>`: точные координаты цитат, числа/единицы, периоды и алиасы, частичные результаты и диагностика. [NORMALIZATION.md](llm/events/NORMALIZATION.md) описывает контракт для B5, правило ближайшего года относительно публикации и явный repair без автоматических вызовов. Сам B4 не требует миграции; B5 добавляет схему 3. Проверка не доказывает истинность и полноту извлечения.

B5 создаёт каталог из явно выбранных артефактов B4: `python -m llm.events.catalog_cli init`, затем `plan/apply --artifact-id <b4_id>`. [CATALOG.md](llm/events/CATALOG.md) описывает миграцию v2 → v3, историю событий, ручной пересмотр, явную LLM-рекомендацию и ограниченную полноту правил на 15 статьях. Текущий каталог блокируется при смене редакции до явной пересборки.


## Запуск Backend (FastAPI)

Из папки `backend/`:

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Интерактивная Swagger-документация: `http://localhost:8000/docs`

## Запуск Frontend (Vite + React)

Нужен Node.js 20 или новее. Из папки `frontend/`:

```bash
cd frontend
npm install
npm run dev
```

Интерфейс откроется на `http://localhost:5173`. По умолчанию он работает на моках в браузере, backend и ключ LLM не нужны.

Чтобы подключить backend, скопируйте `frontend/.env.example` в `frontend/.env` и поставьте `VITE_API_MODE=http`. Адрес API берётся из `VITE_API_URL` (по умолчанию `http://localhost:8000`), сам backend запускается по инструкции выше. Какие данные и в каком виде фронт ждёт от backend, описано в [docs/frontend-api-contract.md](docs/frontend-api-contract.md). Пока заглушки backend отдают ответы в старом формате, так что для демо лучше оставить режим `mock`.

Подробнее: [frontend/README.md](frontend/README.md).

## Запуск через Docker

Нужны Docker и Docker Compose v2.24 или новее (подойдёт Docker Desktop). Из корня проекта:

```bash
cp .env.example .env      # необязательно: значения по умолчанию рабочие
docker compose up --build
```

Когда оба сервиса станут healthy, интерфейс откроется на `http://localhost:8080`, Swagger backend на `http://localhost:8000/docs`.

Что поднимается:

- `backend`: FastAPI, база SQLite лежит в томе `backend-data` и переживает пересоздание контейнера.
- `frontend`: nginx раздаёт собранный интерфейс и проксирует `/api/` на backend, поэтому фронт и API живут на одном адресе без CORS. Фронт стартует только после того, как backend стал healthy.

Откуда фронт берёт данные, задаёт `APP_API_MODE` в `.env`:

- `mock` (по умолчанию): всё считается в браузере, backend не нужен;
- `http`: фронт ходит в backend через `/api/`. Менять режим можно без пересборки: поправьте `.env` и выполните `docker compose up -d`. Пока заглушки backend отдают ответы в старом формате, в этом режиме откроется экран «Не удалось показать данные»; формат ответов описан в [docs/frontend-api-contract.md](docs/frontend-api-contract.md).

Полезное:

```bash
docker compose logs -f backend     # логи
docker compose down                # остановить, база сохранится
docker compose down -v             # остановить и удалить базу
sh backend/update-lock.sh          # пересобрать backend/requirements.lock после правки requirements.txt
```

Контейнеры работают без root, у backend файловая система только для чтения (пишет лишь в `/data`), порт backend открыт только на `127.0.0.1`. Порты меняются в `.env` (`FRONTEND_PORT`, `BACKEND_PORT`). Ключи LLM кладите только в `.env`: он в `.gitignore` и попадает только в контейнер backend.

## Документация

- [Контракт API для фронтенда](docs/frontend-api-contract.md) и [примеры ответов](docs/api-examples/)
- [Контракт выхода NLP-блока B6 для аналитики компании](docs/b6-event-contract.md), JSON Schema и синтетические примеры событий
- [Использованные компоненты и материалы](THIRD_PARTY_NOTICES)
