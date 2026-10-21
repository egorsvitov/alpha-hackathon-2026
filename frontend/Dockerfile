# syntax=docker/dockerfile:1

# ---------- Сборка ----------
FROM node:24-alpine AS build
WORKDIR /app

# Сначала только манифесты: слой с зависимостями не пересобирается при правках кода.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
# Адрес и режим API задаются при старте контейнера (config.js), а не при сборке.
RUN npm run build

# ---------- Раздача ----------
# nginx без root: слушает 8080, работает от пользователя nginx (uid 101).
FROM nginxinc/nginx-unprivileged:1.27-alpine

ENV BACKEND_URL=http://backend:8000 \
    DNS_RESOLVER=127.0.0.11 \
    APP_API_MODE=mock \
    APP_API_URL=

COPY --chown=101:101 nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --chown=101:101 --chmod=755 nginx/40-app-config.sh /docker-entrypoint.d/40-app-config.sh
COPY --from=build --chown=101:101 /app/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
