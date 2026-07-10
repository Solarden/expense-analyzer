# Expense Analyzer

Locally hosted, self-managed household finance analyzer. Aggregates bank
transactions, investment positions and loans in one place, with a web dashboard
and glanceable metrics pushed to Home Assistant. Runs on a Raspberry Pi next to
Home Assistant, LAN-only.

## Features

- Import bank transactions from CSV (idempotent — re-importing a file never
  duplicates rows).
- Money stored as integer minor units, never float — balances always reconcile.
- Categorize expenses and tag them private vs household.
- Track loans with repayment schedules and investment positions (informational).
- Detect internal transfers between own accounts and keep them out of the
  spending/income figures.
- Per-category monthly budgets and recurring-payment detection.
- Push glanceable metrics and alerts to Home Assistant over MQTT.

All financial data stays on your own hardware — nothing leaves for the cloud.

## Stack

- Python 3.11+ / FastAPI
- PostgreSQL (production; any existing server on your LAN — not part of this
  compose) or SQLite (zero-setup local dev), SQLModel, Alembic
- Jinja2 + Chart.js (server-rendered dashboard; no HTMX/SPA)
- docker compose: `app`, `worker`, `caddy`
- Dependencies managed with [uv](https://docs.astral.sh/uv/)

## Local development

```bash
uv sync --extra dev          # create .venv and install deps
uv run alembic upgrade head  # apply migrations
make dev                     # run with autoreload (sets EA_DEBUG so the dev default secret is allowed)
```

`make dev` is shorthand for `EA_DEBUG=true uv run uvicorn expense_analyzer.main:app --reload`.
Outside debug mode the app refuses to start on the insecure default secret, so set
`EA_SECRET_KEY` if you run uvicorn directly without `EA_DEBUG`.

App: <http://127.0.0.1:8000> — health check at `/health`. Create a login user with
`make user u=you n="You"` (or `make seed` for the demo dataset below) before signing in.

### Demo data

To explore the app with something to click through, load a demo dataset — a few
accounts, ~4 months of transactions, a variable-rate mortgage, an investment
snapshot, budgets, planned cashflow items, rules, and a pending "update available"
notification (so **System → Updates** has something to show):

```bash
make migrate   # if you haven't already
make seed      # reset the local DB to the demo dataset
make dev
```

Then sign in as **`admin` / `demo1234`**. `make seed` wipes the data tables and
rebuilds from scratch each run, so it's safe to re-run. It refuses to run
against anything but a local SQLite file, so a production `EA_DATABASE_URL`
sitting in `.env` can't be wiped by accident.

Run the tests (against a throwaway Postgres container — prod parity):

```bash
make test        # starts the test DB (docker) and runs pytest
make test-db-down  # stop the test DB and discard its data
```

For a quick docker-less run, point the suite at SQLite instead:

```bash
EA_TEST_DATABASE_URL=sqlite:///$(mktemp -d)/test.db uv run pytest
```

Enable the git pre-commit hooks (ruff, secret scanning, Dockerfile lint, and
general hygiene) once after cloning:

```bash
uv run pre-commit install
uv run pre-commit run --all-files   # optional: run against the whole repo
```

## Running with Docker

The stack expects an existing PostgreSQL server on your LAN (it is deliberately
NOT a service of this compose — point it at whatever Postgres you already run).
Create the app's role and database there once:

```sql
CREATE ROLE expense_analyzer LOGIN PASSWORD '...';
CREATE DATABASE expense_analyzer OWNER expense_analyzer;
```

Then:

```bash
export EA_DATABASE_URL="postgresql+psycopg://expense_analyzer:...@192.168.1.x:5432/expense_analyzer"
export EA_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export CADDY_SITE_ADDRESS=expense.local   # hostname or LAN IP of the Pi
docker compose up --build
```

The app sits behind Caddy (LAN-only) over **HTTPS** at `https://$CADDY_SITE_ADDRESS`.
Caddy serves a self-signed cert from its own local CA (no internet contact) —
install that root CA on your devices to avoid browser warnings (the CA lives in
the `caddy_data` volume at `/data/caddy/pki/authorities/local/root.crt`). The
schema is created on first boot (`alembic upgrade head`); file data (loan
attachments, update status, backups) lives on the `./data` bind mount.

Create the first login user, then sign in:

```bash
docker compose run --rm app python -m expense_analyzer.create_user --username you --name "You"
```

## Deploy on the Pi

Once the stack is running, ship a new version with a single command:

```bash
make deploy a="--pull"   # git pull, then deploy
make deploy              # deploy the code already checked out
```

`scripts/deploy.sh` builds the new images, **backs up the database before
anything migrates it**, restarts the stack (the `app` container runs
`alembic upgrade head` on boot), and waits for the health check. If the app
doesn't come up healthy it **rolls back** — restoring the database copy and
re-tagging the previous image. It's idempotent and safe to re-run. Preview the
plan with `make deploy a="--dry-run"`.

The only network egress is the optional `git pull` from this repo and the
docker build fetching base layers — no Watchtower, no registry auto-pull.

### Backups

```bash
make backup   # write a timestamped copy to data/backups
```

On PostgreSQL the backup is a `pg_dump --format=custom` archive (`.dump`),
taken from inside the app image so the client always matches the deploy; on a
SQLite dev database it's the online backup API (a consistent single `.db` file
even while the app is writing). Wire `make backup` into cron on the Pi for
periodic copies (design §10); `EA_BACKUP_KEEP` caps how many are retained.
Restore (stop the stack first — the restore resets the schema under whatever
is connected):

```bash
docker compose down
docker compose run --rm --no-deps -T app \
  python -m expense_analyzer.backup --restore /data/backups/<file>
docker compose up -d
```

Under the hood it validates the archive, resets the `public` schema, then runs
`pg_restore` — the deploy's rollback uses the same path. The image pins
`postgresql-client-18`; keep that major in lockstep with your server's (a newer
client dumps older servers fine, but restoring across majors can report
spurious failures).

### Update notifications

```bash
make check-update   # is a newer release tagged? notify Home Assistant
```

`scripts/check_update.sh` fetches tags from this repo and, if a newer
**release tag** exists than the one deployed, publishes a retained
`sensor.expense_analyzer_update` to Home Assistant (with `current` /
`update_available` attributes), fires an alert, and writes the verdict to
`data/update_status.json`. The dashboard surfaces it read-only at **System →
Updates** (`/dashboard/updates`) — "a new version is available, run `make deploy`",
with a link to the release; the page only *reads* that file and never contacts the
network itself. It is **notify-only** — it never deploys; you run `make deploy`
when you choose. The only egress is the git
fetch of our own repo (maintenance, not runtime — no Watchtower, no registry).

This relies on release tags: tag what you want to ship with `git tag vX.Y.Z`
(plain `vMAJOR.MINOR.PATCH`; pre-releases like `v1.4.0-rc1` are ignored). Until
the first tag exists the check is a no-op. **Forked it?** Point the check at your
own repo with `EA_UPDATE_REMOTE` in `.env` (a git remote name like `origin`/
`upstream`, or a full URL) — it defaults to `origin`. Run it periodically via a
systemd timer or cron on the Pi, e.g.:

```cron
# /etc/cron.d/expense-analyzer-update — check for a new release each morning
30 7 * * *  pi  cd /home/pi/expense-analyzer && make check-update >> data/check-update.log 2>&1
```

## Migrations

```bash
uv run alembic revision --autogenerate -m "add transaction model"
uv run alembic upgrade head
```

Models live in `src/expense_analyzer/models.py`; Alembic autogenerate targets
`SQLModel.metadata`.

## License

Copyright (C) 2026 **Pawel Chraczynski** (GitHub: [@Solarden](https://github.com/Solarden)) —
sole author and copyright holder.

Expense Analyzer is free software under the **GNU AGPL v3 or later**
(`AGPL-3.0-or-later`) — see [LICENSE](LICENSE). In short: use, modify and share
freely (run it at home all you want), but if you distribute it or run a modified
version as a network service, you must release your source under the same terms.

Need terms the AGPL doesn't allow (e.g. closed-source or commercial use)? A
separate **commercial license** is available — see [LICENSING.md](LICENSING.md)
or email **contact@szawel.com**.
