#!/bin/sh
# Пишет /usr/share/nginx/html/config.js из переменных окружения при старте контейнера.
# Так режим данных (mock или http) и адрес API меняются без пересборки образа.
#   APP_API_MODE — mock (по умолчанию) или http
#   APP_API_URL  — адрес API; пусто = тот же адрес, что у сайта (nginx проксирует /api/ на backend)
set -eu

mode="${APP_API_MODE:-mock}"
case "$mode" in
  mock|http) ;;
  *) echo "40-app-config: APP_API_MODE должен быть mock или http, получено '$mode'; использую mock" >&2; mode="mock" ;;
esac

# Убираем символы, которые могли бы сломать JavaScript-строку.
url="$(printf '%s' "${APP_API_URL:-}" | tr -d '"\\<>`$' | tr -d '\n\r')"

cat > /usr/share/nginx/html/config.js <<EOF
window.__APP_CONFIG__ = { apiMode: "${mode}", apiUrl: "${url}" };
EOF

echo "40-app-config: apiMode=${mode} apiUrl='${url}'"
