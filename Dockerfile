# syntax=docker/dockerfile:1
#
# mcp-google-tasks: Google Tasks MCP server, packaged for container runtimes such as
# Obot's containerized single-user catalog entries. Serves streamable HTTP on port 8000 at
# /mcp, with an unauthenticated GET /health for liveness checks.
#
# The image defaults to MCP_TRANSPORT=http. MCP_AUTH_MODE then defaults to oauth-proxy, so a
# single-user deployment must set MCP_AUTH_MODE=token plus the GOOGLE_OAUTH_* variables (see
# catalog/google-tasks.yaml and docs/DESIGN.md).

# ---------------------------------------------------------------------------------------------
# Builder: resolve and install the locked dependency set into /app/.venv.
# ---------------------------------------------------------------------------------------------
# Both stages name the base image literally (not via ARG) so Dependabot can bump them.
FROM python:3.14-slim AS builder

# Pinned to the uv release that wrote uv.lock, so `--locked` never trips over a lockfile
# format change; bump it together with the lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

# - UV_COMPILE_BYTECODE: compile .pyc at build time so the non-root runtime never needs to
#   write them.
# - UV_LINK_MODE=copy: the cache is a separate mount, so hard links would fail.
# - UV_PYTHON_DOWNLOADS=never / UV_PYTHON: use the image's interpreter, which the runtime
#   stage provides at the same path, so the venv's symlinks stay valid after the copy.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.14 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml or uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-editable --no-install-project

# Then the project itself, installed as a regular (non-editable) wheel so the runtime stage
# needs only the venv, not the source tree. README.md and LICENSE are part of the package
# metadata (readme, license-files), so the build backend needs them.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------------------------
# Runtime: same base image, venv only, non-root.
# ---------------------------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

LABEL org.opencontainers.image.title="mcp-google-tasks" \
      org.opencontainers.image.description="Google Tasks MCP server (multi-user OAuth or single-user token)" \
      org.opencontainers.image.source="https://github.com/dcode/mcp-google-tasks" \
      org.opencontainers.image.url="https://github.com/dcode/mcp-google-tasks" \
      org.opencontainers.image.licenses="Apache-2.0"

# A fixed numeric uid/gid, so orchestrators can verify runAsNonRoot and volumes can be
# pre-provisioned with matching ownership.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir -p /data/oauth-tokens \
    && chown -R 10001:10001 /data

WORKDIR /app

# Owned by root and read-only to the app user: the server never modifies its own install.
COPY --from=builder /app/.venv /app/.venv

# TOKEN_STORAGE_DIR is a directory path, not a secret (hadolint matches on the word "TOKEN").
# hadolint ignore=DL3064
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    TOKEN_STORAGE_DIR=/data/oauth-tokens

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000

# urlopen raises on connection errors and non-2xx responses, which exits non-zero.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT', '8000') + '/health', timeout=4)"]

ENTRYPOINT ["/app/.venv/bin/mcp-google-tasks"]
