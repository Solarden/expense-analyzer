# Multi-stage build using uv. Final image runs on linux/arm64 (Raspberry Pi)
# as well as amd64 dev machines.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS base

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer), then the project.
COPY pyproject.toml ./
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --no-dev

COPY alembic.ini ./
COPY alembic ./alembic

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["uvicorn", "expense_analyzer.main:app", "--host", "0.0.0.0", "--port", "8000"]
