# Новостная аналитика для бизнеса

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
ranking/                   # Профили компаний, отбор и ранжирование
    audit_data.py          # Готовая заготовка аудита CSV
llm/
    rag/                   # Подготовка PDF и поиск доказательств
    agent/                 # Извлечение фактов, интерпретация и самопроверка
        contracts/         # Проект формата ответа и ручной пример
        model_policy.json  # Проект выбора моделей и бюджетной политики
news/                      # Сборщик внешних новостей и профилирование аудитории
    collect.py             # Сбор источников из news/sources.json
    dump_large.py          # Крупная выгрузка корпуса (~10k) для кластеризации
    scheduler.py           # Периодическая проверка новых статей (расписание)
    profile/               # Анализ аудитории → темы, поиск новых источников
        source_discovery.py   # Выделение новых источников под темы аудитории
        audience.py           # Профиль аудитории из CSV клиентов
    sources.json           # Реестр источников новостей
    Dockerfile             # Задача-расписание сбора в Docker (см. docker-compose)
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
- `news-collector`: регулярный сбор новостей по расписанию (см. `news/README.md`). Архив статей пишется в том `news-data` (на VPS монтируется в постоянное хранилище). Интервал — `NEWS_EVERY` (сек, по умолчанию 3600 = 1 час). Разовый проход: `docker compose run --rm news-collector python news/scheduler.py --once`.

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
- [Использованные компоненты и материалы](THIRD_PARTY_NOTICES)
