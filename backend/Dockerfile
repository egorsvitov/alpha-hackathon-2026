# syntax=docker/dockerfile:1
# Python 3.13: на этой версии проверялись остальные модули проекта (см. README).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Без root. /data — том для SQLite, чтобы база переживала пересоздание контейнера.
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir /data \
    && chown app:app /data

WORKDIR /app

# Ставим точные версии из lock-файла (как его обновить, написано в README).
COPY backend/requirements.lock ./
RUN pip install -r requirements.lock

COPY backend ./backend
COPY retriever ./retriever
COPY llm/cluster_merge ./llm/cluster_merge
COPY llm/event_pipeline ./llm/event_pipeline
COPY ranking ./ranking
ENV PYTHONPATH=/app:/app/backend

ENV DATABASE_URL=sqlite+aiosqlite:////data/alfa_hack.db

USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=2)"]

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
