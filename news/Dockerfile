# syntax=docker/dockerfile:1
# Сборщик новостей (пункт 2) — регулярный анализ аудитории, поиск и архив новостей.
# Запускает news/scheduler.py, который периодически собирает статьи по расписанию.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Без root. /data — том для архива новостей и кэша, чтобы переживал пересоздание контейнера.
RUN useradd --system --uid 10002 --no-create-home collector \
    && mkdir -p /data/news_bulk /data/news_cache /data/news_state \
    && chown -R collector:collector /data

WORKDIR /app

# Ставим точные версии из lock-файла.
COPY news/requirements.lock ./requirements.lock
RUN pip install -r requirements.lock

COPY news ./news

# Конфигурация сбора. Переопределяется через .env / compose.
ENV NEWS_OUT_DIR=/data/news_bulk \
    NEWS_CACHE_DIR=/data/news_cache \
    NEWS_STATE=/data/news_state/schedule_state.json \
    NEWS_EVERY=3600

USER collector

# По умолчанию крутим расписание (каждый час). Для разового прохода:
#   docker run ... <image> python news/scheduler.py --once
CMD ["python", "news/scheduler.py", "--every", "3600"]