#!/usr/bin/env bash
# One-command deploy for the Raspberry Pi (Phase 18).
#
#   1. (optional) git pull --ff-only from OUR OWN repo            [--pull]
#   2. build the new images
#   3. back up the database  ── BEFORE any migration ──           (design §10)
#   4. docker compose up -d  → the app runs `alembic upgrade head` on boot
#   5. wait for the app to report healthy
#   6. on failure: ROLL BACK — restore the DB backup and re-tag the previous image
#
# Idempotent and safe to re-run. The only network egress is the optional git
# pull from our own repo and the docker build fetching base layers — no
# Watchtower, no registry auto-pull (keep-pi-fully-local).
#
# Usage:
#   scripts/deploy.sh [--pull] [--keep N] [--dry-run]
#     --pull       git pull --ff-only before deploying (requires a clean tree)
#     --keep N     keep N newest DB backups (default: EA_BACKUP_KEEP or 14)
#     --dry-run    print the plan and exit without touching anything
set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

COMPOSE="docker compose"
APP_SERVICE="app"
WORKER_SERVICE="worker"
HEALTH_INTERVAL=3
KEEP=""
DO_PULL=false
DRY_RUN=false

log() { printf '\033[36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[deploy] WARNING:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Read KEY=value from .env (ignoring comments); empty if absent. This shell
# script, unlike docker compose, doesn't auto-load .env, so the Phase 18 ops
# knobs wouldn't take effect from .env without this. Env vars / flags win.
dotenv_get() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n1 || true; }

while [ $# -gt 0 ]; do
  case "$1" in
    --pull) DO_PULL=true ;;
    --keep) shift; KEEP="${1:?--keep needs a number}" ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# Precedence: flag > env var > .env > built-in default.
KEEP="${KEEP:-${EA_BACKUP_KEEP:-$(dotenv_get EA_BACKUP_KEEP)}}"
KEEP="${KEEP:-14}"
HEALTH_TIMEOUT="${EA_DEPLOY_HEALTH_TIMEOUT:-$(dotenv_get EA_DEPLOY_HEALTH_TIMEOUT)}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"

# --- preflight --------------------------------------------------------------
PREV_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"

if $DRY_RUN; then
  log "DRY RUN — no changes will be made"
  log "would pull:        $DO_PULL"
  log "current revision:  $PREV_SHA"
  log "backups to keep:   $KEEP"
  log "health timeout:    ${HEALTH_TIMEOUT}s"
  log "steps: pull? -> build -> backup DB -> up -d (migrate) -> health -> rollback on failure"
  exit 0
fi

command -v docker >/dev/null || die "docker not found on PATH"
$COMPOSE version >/dev/null 2>&1 || die "'docker compose' v2 not available"
[ -f .env ] || warn ".env not found — relying on the ambient environment (EA_SECRET_KEY etc.)"

# --- 1. optional git pull (our own repo only) -------------------------------
if $DO_PULL; then
  [ -z "$(git status --porcelain)" ] || die "working tree is dirty — commit/stash before --pull"
  log "git pull --ff-only (was $PREV_SHA)"
  git pull --ff-only
  log "now at $(git rev-parse --short HEAD)"
fi

# Record, BEFORE the build, both the image ID currently backing each build
# service and the REPOSITORY:TAG name pointing at it — so a failed deploy can
# retag the old ID back onto that name without a rebuild. Captured pre-build on
# purpose: the build moves the tag to the new image, after which the old running
# container's image can read as <none>:<none>, losing the retag target. The name
# itself doesn't change across a build, so the pre-build name is the stable one.
prev_image_id() { $COMPOSE images -q "$1" 2>/dev/null | head -n1; }
image_ref() { $COMPOSE images "$1" 2>/dev/null | awk 'NR==2 {print $2 ":" $3}'; }

PREV_APP_IMAGE="$(prev_image_id "$APP_SERVICE")"
PREV_WORKER_IMAGE="$(prev_image_id "$WORKER_SERVICE")"
APP_IMAGE_REF="$(image_ref "$APP_SERVICE")"
WORKER_IMAGE_REF="$(image_ref "$WORKER_SERVICE")"

# --- 2. build ---------------------------------------------------------------
log "building images"
$COMPOSE build

# --- 3. backup the DB (before the new app container migrates it) ------------
# One run inside the freshly built image (the host needs only docker — pg_dump
# is in the image and the DB server is the shared /opt/stack Postgres). Backups
# land in /data/backups, i.e. ./data/backups on the host via the bind mount. We
# invoke the backup module directly, so this does NOT trigger a migration.
# --if-exists turns a first deploy with no database into a clean skip (empty
# stdout) rather than an error; a genuine backup failure exits non-zero and
# aborts the deploy here, before anything is migrated (set -e + pipefail).
BACKUP=""
log "backing up database (keep $KEEP)"
CONTAINER_BACKUP="$($COMPOSE run --rm --no-deps -T "$APP_SERVICE" \
  python -m expense_analyzer.backup --keep "$KEEP" --if-exists | tail -n1 | tr -d '\r')"
if [ -n "$CONTAINER_BACKUP" ]; then
  # Map the container path (/data/...) to its host equivalent under ./data.
  BACKUP="data/${CONTAINER_BACKUP#/data/}"
  [ -f "$BACKUP" ] || warn "backup reported at $CONTAINER_BACKUP but not found on host at $BACKUP"
  log "backup: $BACKUP"
else
  log "no database yet — first deploy, skipping backup"
fi

# --- rollback ---------------------------------------------------------------
# Called explicitly on any post-build failure (never via an ERR trap — our
# failures go through die(), and `exit` does not fire ERR). Disables `set -e` so
# a hiccup in one recovery step doesn't abort the rest of the recovery.
rollback() {
  set +e
  warn "deploy failed: $1 — rolling back"
  $COMPOSE down --remove-orphans

  if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
    warn "restoring database from $BACKUP"
    # The module is dialect-aware (on Postgres: schema reset + pg_restore). Map
    # the host path back to its container equivalent under the ./data bind mount.
    $COMPOSE run --rm --no-deps -T "$APP_SERVICE" \
      python -m expense_analyzer.backup --restore "/$BACKUP" \
      || warn "database restore failed — restore manually from $BACKUP"
  else
    warn "no backup to restore (the database is unchanged from before the deploy)"
  fi

  rolled_back=false
  if [ -n "$PREV_APP_IMAGE" ] && [ -n "$APP_IMAGE_REF" ] && [ "$APP_IMAGE_REF" != ":" ]; then
    if docker tag "$PREV_APP_IMAGE" "$APP_IMAGE_REF"; then rolled_back=true; fi
  fi
  if [ -n "$PREV_WORKER_IMAGE" ] && [ -n "$WORKER_IMAGE_REF" ] && [ "$WORKER_IMAGE_REF" != ":" ]; then
    docker tag "$PREV_WORKER_IMAGE" "$WORKER_IMAGE_REF"
  fi

  if $rolled_back; then
    warn "restarting on the previous image"
    $COMPOSE up -d || warn "could not restart the previous stack — investigate manually"
  else
    warn "no previous image to roll back to (was this the first deploy?)"
    warn "the database has been restored; fix the build and re-run scripts/deploy.sh"
  fi
  die "rollback complete (previous revision $PREV_SHA, backup kept at ${BACKUP:-none})"
}

# --- 4. start the new stack (migrates on boot) ------------------------------
log "starting stack (migrations run on app boot)"
$COMPOSE up -d || rollback "docker compose up failed"

# --- 5. wait for health -----------------------------------------------------
log "waiting for the app to become healthy (timeout ${HEALTH_TIMEOUT}s)"
deadline=$((SECONDS + HEALTH_TIMEOUT))
cid="$($COMPOSE ps -q "$APP_SERVICE")"
[ -n "$cid" ] || rollback "app container did not start"
while :; do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo unknown)"
  case "$status" in
    healthy) break ;;
    unhealthy) rollback "app reported unhealthy" ;;
  esac
  [ "$SECONDS" -lt "$deadline" ] || rollback "app did not become healthy within ${HEALTH_TIMEOUT}s (status: $status)"
  sleep "$HEALTH_INTERVAL"
done

log "✅ deploy succeeded — app is healthy at revision $(git rev-parse --short HEAD)"
[ -n "$BACKUP" ] && log "pre-deploy backup kept at $BACKUP"
exit 0
