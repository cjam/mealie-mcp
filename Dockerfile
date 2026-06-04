# Production image for the Mealie MCP server (deploy to NAS).
FROM python:3.12-slim AS base

# uv: fast, reproducible installs from uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first for layer caching. uv.lock is optional; if present it is
# used for a reproducible, frozen install, otherwise uv resolves from pyproject.
COPY pyproject.toml uv.lock* ./
RUN if [ -f uv.lock ]; then \
        uv sync --frozen --no-dev --no-install-project; \
    else \
        uv sync --no-dev --no-install-project; \
    fi

# App code.
COPY src/ ./src/

ENV PATH="/app/.venv/bin:$PATH" \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_TRANSPORT=http \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python", "src/server.py"]
