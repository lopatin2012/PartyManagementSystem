#!/usr/bin/env bash
#
# CD-скрипт PartyManagementSystem.
#
# Порядок действий:
#   1. git fetch / checkout / pull --ff-only нужной ветки;
#   2. docker compose build;
#   3. проверки в одноразовом контейнере (gate, останавливает деплой при ошибке):
#        - python manage.py check
#        - python manage.py makemigrations
#        - python manage.py collectstatic --noinput
#   4. применение миграций (default + archive);
#   5. docker compose up -d --build — запуск web / scheduler / worker;
#   6. пост-проверка: python manage.py check в running-контейнере.
#
# Использование:
#   ./deploy.sh [branch] [--check-only] [--no-pull] [--no-build]
#
# Примеры:
#   ./deploy.sh                      # текущая ветка
#   ./deploy.sh main                 # ветка main
#   ./deploy.sh main --check-only    # только проверки, без миграций и запуска
#
# Переменные окружения:
#   SERVICE         имя сервиса web      (по умолчанию web)
#   DB_SERVICE      имя сервиса БД       (по умолчанию db)
#   HEALTH_TIMEOUT  секунд ожидания БД   (по умолчанию 90)

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE="${SERVICE:-web}"
DB_SERVICE="${DB_SERVICE:-db}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"

BRANCH=""
CHECK_ONLY=0
NO_PULL=0
NO_BUILD=0

C_RESET='\033[0m'
C_INFO='\033[36m'
C_OK='\033[32m'
C_WARN='\033[33m'
C_FAIL='\033[31m'

log()  { printf "${C_INFO}[deploy]${C_RESET} %s\n" "$*"; }
ok()   { printf "${C_OK}[  ok  ]${C_RESET} %s\n" "$*"; }
warn() { printf "${C_WARN}[ warn ]${C_RESET} %s\n" "$*"; }
die()  { printf "${C_FAIL}[ fail ]${C_RESET} %s\n" "$*" >&2; exit 1; }

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check-only) CHECK_ONLY=1 ;;
    --no-pull)    NO_PULL=1 ;;
    --no-build)   NO_BUILD=1 ;;
    -h|--help)    usage ;;
    --*)          die "Неизвестный параметр: $1" ;;
    *)
      [[ -z "$BRANCH" ]] || die "Ветка уже задана: $BRANCH"
      BRANCH="$1"
      ;;
  esac
  shift
done

# --- 0. Проверка окружения -------------------------------------------------
command -v git >/dev/null 2>&1 || die "git не найден в PATH"
command -v docker >/dev/null 2>&1 || die "docker не найден в PATH"
docker compose version >/dev/null 2>&1 || die "docker compose не найден"

git rev-parse --git-dir >/dev/null 2>&1 || die "Каталог $SCRIPT_DIR не является git-репозиторием"

if [[ -z "$BRANCH" ]]; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD)"
fi
[[ "$BRANCH" == "HEAD" ]] && die "Обнаружен detached HEAD. Укажите ветку: ./deploy.sh <branch>"

log "Ветка деплоя: ${BRANCH}"

# --- 1. Обновление кода ----------------------------------------------------
if [[ "$NO_PULL" -eq 0 ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    warn "В репозитории есть незакоммиченные изменения tracked-файлов:"
    git status --short --untracked-files=no
    warn "Продолжаю, но git pull может упасть."
  fi

  log "git fetch origin ${BRANCH}"
  git fetch origin "$BRANCH"

  if [[ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]]; then
    log "git checkout ${BRANCH}"
    git checkout "$BRANCH"
  fi

  log "git pull --ff-only origin ${BRANCH}"
  git pull --ff-only origin "$BRANCH"
  ok "Код обновлён: $(git rev-parse --short HEAD) ($(git log -1 --pretty=%s))"
else
  warn "Пропускаю git pull (--no-pull)"
fi

# --- 2. Сборка образа ------------------------------------------------------
if [[ "$NO_BUILD" -eq 0 ]]; then
  log "docker compose build ${SERVICE}"
  docker compose build "$SERVICE"
  ok "Образ собран"
else
  warn "Пропускаю сборку (--no-build)"
fi

# --- 3. Запуск БД и ожидание готовности ------------------------------------
log "Запуск сервиса БД '${DB_SERVICE}'"
docker compose up -d "$DB_SERVICE"

log "Ожидание готовности БД (до ${HEALTH_TIMEOUT}s)"
DB_CID="$(docker compose ps -q "$DB_SERVICE")"
[[ -n "$DB_CID" ]] || die "Не удалось найти контейнер сервиса '${DB_SERVICE}'"

waited=0
while true; do
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$DB_CID" 2>/dev/null || echo unknown)"
  case "$health" in
    healthy|running) ok "БД готова (${health})"; break ;;
    unhealthy) die "БД перешла в состояние unhealthy" ;;
  esac
  if (( waited >= HEALTH_TIMEOUT )); then
    die "БД не готова за ${HEALTH_TIMEOUT}s (status: ${health})"
  fi
  sleep 2
  waited=$(( waited + 2 ))
done

# --- 4. Gate: проверка проекта --------------------------------------------
log "Проверки проекта (gate)"
docker compose run --rm --entrypoint sh "$SERVICE" -c '
  set -e
  echo ">>> python manage.py check"
  python manage.py check

  echo ">>> python manage.py makemigrations"
  python manage.py makemigrations

  echo ">>> python manage.py collectstatic --noinput"
  python manage.py collectstatic --noinput
'
ok "Проверки пройдены (check / makemigrations / collectstatic)"

# --- 5. Применение миграций ------------------------------------------------
if [[ "$CHECK_ONLY" -eq 0 ]]; then
  log "Применение миграций"
  docker compose run --rm --entrypoint sh "$SERVICE" -c '
    set -e
    echo ">>> python manage.py migrate"
    python manage.py migrate

    echo ">>> python manage.py migrate --database archive"
    python manage.py migrate --database archive
  '
  ok "Миграции применены"
else
  warn "Пропускаю миграции и запуск (--check-only)"
fi

# --- 6. Запуск сервисов ----------------------------------------------------
if [[ "$CHECK_ONLY" -eq 0 ]]; then
  log "docker compose up -d --build"
  docker compose up -d --build
  ok "Сервисы запущены"

  log "Ожидание запуска контейнера '${SERVICE}'"
  waited=0
  while true; do
    WEB_CID="$(docker compose ps -q "$SERVICE")"
    running="$(docker inspect -f '{{.State.Running}}' "$WEB_CID" 2>/dev/null || echo false)"
    [[ "$running" == "true" ]] && break
    if (( waited >= HEALTH_TIMEOUT )); then
      die "Контейнер '${SERVICE}' не запустился за ${HEALTH_TIMEOUT}s"
    fi
    sleep 2
    waited=$(( waited + 2 ))
  done

  log "Пост-проверка running-контейнера"
  docker compose exec -T "$SERVICE" python manage.py check
  ok "Пост-проверка пройдена"
fi

echo
ok "Деплой завершён: ветка ${BRANCH}, коммит $(git rev-parse --short HEAD)"
if [[ "$CHECK_ONLY" -eq 0 ]]; then
  docker compose ps
fi
