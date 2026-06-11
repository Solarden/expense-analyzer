# Multi-stage build using uv. Final image runs on linux/arm64 (Raspberry Pi)
# as well as amd64 dev machines.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS base

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# pg_dump/pg_restore for backup.py against the shared /opt/stack PostgreSQL
# (backups run inside this image — the Pi host needs only docker). From PGDG:
# bookworm ships only v15, and pg_dump refuses a server NEWER than itself, so
# pin the newest stable major — it dumps any server up to its own version.
# Bump the pin if the /opt/stack server ever moves past it. (DL3008 ignored:
# the major IS pinned; exact deb revisions would rot on every PGDG point release.)
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
         https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]" \
         "https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer), then the project.
COPY pyproject.toml ./
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Categorization layer 3 (Phase 12): bake the sentence-transformers model into the
# image at build time so the running Pi never reaches the network for it (the
# Chart.js-vendoring principle — fetch once at build, zero egress at runtime). The
# build is the only step allowed to download it; HF_HUB_OFFLINE below makes a
# missing model fail fast at runtime instead of silently phoning home. Pinned to an
# exact commit so the build is reproducible and the weights can't change under us —
# keep the ARGs in sync with EA_EMBEDDINGS_MODEL / EA_EMBEDDINGS_MODEL_REVISION
# (config defaults). Sits right after `uv sync` (depends only on the venv) so an
# alembic or app-source change below doesn't invalidate this ~470 MB layer.
ARG EA_EMBEDDINGS_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
ARG EA_EMBEDDINGS_MODEL_REVISION=e8f8c211226b894fcb81acc59f3b34ba3efd5f42
ENV EA_EMBEDDINGS_MODEL=${EA_EMBEDDINGS_MODEL} \
    EA_EMBEDDINGS_MODEL_REVISION=${EA_EMBEDDINGS_MODEL_REVISION} \
    HF_HOME=/app/.hf-cache
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EA_EMBEDDINGS_MODEL}', revision='${EA_EMBEDDINGS_MODEL_REVISION}')"
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000

CMD ["uvicorn", "expense_analyzer.main:app", "--host", "0.0.0.0", "--port", "8000"]
