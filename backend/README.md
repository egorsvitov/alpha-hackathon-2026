# Backend сервиса «Новостная аналитика для бизнеса»

FastAPI сервис, реализующий контракт API для интеграции с веб-интерфейсом интернет-банка и предоставления персонализированных инсайтов.

## Стек технологий

- Python 3.12+
- FastAPI
- Pydantic v2
- SQLAlchemy / aiosqlite (SQLite)
- Uvicorn

## Архитектура и структура модулей

```text
backend/
├── app/
│   ├── api/
│   │   └── v1/
│   │       └── endpoints.py   # HTTP-эндпоинты API v1
│   ├── core/
│   │   ├── config.py          # Настройки приложения и переменных окружения
│   │   └── database.py        # Инициализация подключения к базе данных SQLite
│   ├── models/
│   │   └── entities.py        # Модели таблиц базы данных
│   ├── schemas/
│   │   └── base.py            # Pydantic-схемы контракта API (DTO)
│   └── services/
│       └── store.py           # Хранилище данных и бизнес-логика ответов
├── Dockerfile                 # Контейнеризация сервиса
├── requirements.txt           # Базовые зависимости
├── requirements.lock          # Зафиксированные версии библиотек
└── main.py                    # Точка входа сервиса FastAPI
```

## Эндпоинты API

Сервис предоставляет эндпоинты в соответствии со спецификацией `docs/frontend-api-contract.md`:

| Метод | Путь | Описание |
|---|---|---|
| `GET` | `/api/v1/companies` | Список профилей компаний для выбора в интерфейсе |
| `GET` | `/api/v1/companies/{company_id}/news-feed?role={role}` | Персонализированная лента событий с ранжированием под выбранную роль |
| `GET` | `/api/v1/news/{news_id}/analysis?company_id={id}&role={role}` | Детальная карточка инсайта (факты, позиция банка, риски, действия, источники) |
| `POST` | `/api/v1/chat/ask` | Уточняющий диалог ЛПР по контексту события и профиля |
| `POST` | `/api/v1/news` | Загрузка и первичная обработка нового пользовательского события |
| `POST` | `/api/v1/feedback` | Прием пользовательской оценки полезности аналитики |

## Локальный запуск

1. Перейдите в директорию `backend`:

```bash
cd backend
```

2. Установите зависимости:

```bash
pip install -r requirements.txt
```

3. Запустите сервер разработки:

```bash
uvicorn main:app --reload --port 8000
```

Документация Swagger UI доступна по адресу: `http://localhost:8000/docs`.

## Запуск в Docker

Сервис сконфигурирован для сборки и изолированного запуска в контейнере:

```bash
docker build -t alfa-news-backend:local .
docker run -p 8000:8000 alfa-news-backend:local
```

В составе всего проекта запуск осуществляется через корневой `docker-compose.yml`:

```bash
docker compose up --build
```
