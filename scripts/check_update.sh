#!/usr/bin/env bash
# Check our OWN git repo for a newer release tag and notify Home Assistant if one
# is waiting (Phase 18). Notify-only — it never deploys; you run `make deploy`
# when you choose. Wire it into cron / a systemd timer on the Pi (see README).
#
#   git fetch --tags  →  compare deployed tag (reachable from HEAD) vs newest tag
#   →  publish the verdict to HA over MQTT (retained sensor + alert if behind)
#
# The only egress is the git fetch from our own remote (maintenance, not runtime
# — the blessed exception to keep-pi-fully-local). HA publishing runs inside the
# app image, so the host needs nothing but docker + git.
#
# The remote it checks is configurable (EA_UPDATE_REMOTE in .env, or --remote) so
# a fork can point the update check at its own repo — a git remote NAME (e.g.
# `upstream`) or a full URL both work.
#
# Usage: scripts/check_update.sh [--remote origin] [--dry-run]
set -euo pipefail

cd "$(dirname "$0")/.."  # repo root

COMPOSE="docker compose"
DRY_RUN=false
REMOTE=""

log() { printf '\033[36m[check-update]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[check-update] WARNING:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m[check-update] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Read KEY=value from .env (ignoring comments); empty if absent. This shell
# script, unlike docker compose, doesn't auto-load .env, so the Phase 18 ops
# knobs (EA_UPDATE_REMOTE here) wouldn't take effect from .env without this. A
# real environment variable and the CLI flag both take precedence.
dotenv_get() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n1 || true; }

while [ $# -gt 0 ]; do
  case "$1" in
    --remote) shift; REMOTE="${1:?--remote needs a name}" ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# Precedence: --remote flag > EA_UPDATE_REMOTE env > .env > "origin".
REMOTE="${REMOTE:-${EA_UPDATE_REMOTE:-$(dotenv_get EA_UPDATE_REMOTE)}}"
REMOTE="${REMOTE:-origin}"

if $DRY_RUN; then
  log "DRY RUN — would fetch tags from '$REMOTE' and publish the verdict to HA; not connecting"
  exit 0
fi

command -v git >/dev/null || die "git not found on PATH"

log "fetching tags from $REMOTE"
git fetch --tags --quiet "$REMOTE"

# Deployed release = the most recent tag reachable from HEAD (deploy.sh builds
# from the checkout, so HEAD == what's running). A brand-new release tag sits on
# a commit NOT yet pulled, so it is unreachable here and stays out of "current".
CURRENT="$(git describe --tags --abbrev=0 2>/dev/null || true)"
TAGS="$(git tag --list 'v*')"

if [ -z "$TAGS" ]; then
  log "no release tags yet — tag a release (e.g. git tag v1.0.0) to enable update checks"
  exit 0
fi

log "deployed: ${CURRENT:-untagged}; known release tags: $(echo "$TAGS" | tr '\n' ' ')"

# Hand the comparison + HA publish to the app image (it has the MQTT client,
# config, and LAN access to the broker). The host stays git-only.
printf '%s\n' "$TAGS" | $COMPOSE run --rm --no-deps -T "app" \
  python -m expense_analyzer.ha.update_notify --current "$CURRENT"
