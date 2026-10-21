#!/bin/sh
# Пересобирает requirements.lock из requirements.txt внутри python:3.13-slim:
# те же Python и платформа, что в образе. Запускать после правки requirements.txt:
#   sh backend/update-lock.sh
set -eu
cd "$(dirname "$0")"

tmp="$(mktemp)"
{
  echo "# Зафиксированные версии зависимостей backend (Python 3.13, Linux). Образ ставит именно их."
  echo "# Не редактировать руками: файл создаёт 'sh backend/update-lock.sh' из requirements.txt."
  docker run --rm -i python:3.13-slim sh -c \
    'cat > /tmp/r.txt && pip install -q --disable-pip-version-check --root-user-action=ignore -r /tmp/r.txt >/dev/null && pip freeze' \
    < requirements.txt
} > "$tmp"
mv "$tmp" requirements.lock
echo "requirements.lock обновлён: $(grep -c '==' requirements.lock) пакетов"
