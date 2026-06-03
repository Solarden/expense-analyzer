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
- SQLite (WAL mode), SQLModel, Alembic
- HTMX + Jinja2 + Chart.js (dashboard)
- docker compose: `app`, `worker`, `caddy`
- Dependencies managed with [uv](https://docs.astral.sh/uv/)

## Local development

```bash
uv sync --extra dev          # create .venv and install deps
uv run alembic upgrade head  # apply migrations
uv run uvicorn expense_analyzer.main:app --reload
```

App: <http://127.0.0.1:8000> — health check at `/health`.

Run the tests:

```bash
uv run pytest
```

Enable the git pre-commit hooks (ruff, secret scanning, Dockerfile lint, and
general hygiene) once after cloning:

```bash
uv run pre-commit install
uv run pre-commit run --all-files   # optional: run against the whole repo
```

## Running with Docker

```bash
export EA_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export CADDY_SITE_ADDRESS=expense.local   # hostname or LAN IP of the Pi
docker compose up --build
```

The app sits behind Caddy (LAN-only) over **HTTPS** at `https://$CADDY_SITE_ADDRESS`.
Caddy serves a self-signed cert from its own local CA (no internet contact) —
install that root CA on your devices to avoid browser warnings (the CA lives in
the `caddy_data` volume at `/data/caddy/pki/authorities/local/root.crt`). The
database lives in `./data/expense_analyzer.db`, mounted into the containers.

Create the first login user, then sign in:

```bash
docker compose run --rm app python -m expense_analyzer.create_user --username you --name "You"
```

## Migrations

```bash
uv run alembic revision --autogenerate -m "add transaction model"
uv run alembic upgrade head
```

Models live in `src/expense_analyzer/models.py`; Alembic autogenerate targets
`SQLModel.metadata`.

## License

See [LICENSE](LICENSE).
