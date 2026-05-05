# syntax=docker/dockerfile:1
# Home Assistant MCP Server - Production Docker Image
# Multi-stage build: uv for dependency resolution, slim Python for runtime
# Python 3.13 - Security support until 2029-10
# Base images pinned by digest - Renovate will create PRs for updates

# --- Build stage: install dependencies with uv ---
FROM ghcr.io/astral-sh/uv:0.11.0-python3.13-trixie-slim@sha256:5b216b72b3bc10f983f82b39b5386455bfa08d2139afb4cb3f6c9f060484ea5d AS builder

WORKDIR /app

# Compile bytecode for faster startup; copy mode required with cache mounts
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first (cached separately from source changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# Copy source and config, then install the project itself
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- Runtime stage: clean image without uv ---
FROM python:3.13-slim@sha256:3de9a8d7aedbb7984dc18f2dff178a7850f16c1ae7c34ba9d7ecc23d0755e35f

LABEL org.opencontainers.image.title="Home Assistant MCP Server" \
      org.opencontainers.image.description="AI assistant integration for Home Assistant via Model Context Protocol" \
      org.opencontainers.image.source="https://github.com/homeassistant-ai/ha-mcp" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.homeassistant-ai/ha-mcp"

# Create non-root user. /home/mcpuser is mode 0755 (not the default 0700) so
# that callers running with `--user UID:GID` overrides — common in hardened
# Docker setups, see issue #1125 — can stat HOME-relative paths. Write
# access stays restricted to mcpuser via ownership.
RUN groupadd -r mcpuser \
    && useradd -r -g mcpuser -m mcpuser \
    && chmod 0755 /home/mcpuser

WORKDIR /app

# Copy the virtual environment, source, and config from builder
COPY --chown=mcpuser:mcpuser --from=builder /app/.venv /app/.venv
COPY --chown=mcpuser:mcpuser --from=builder /app/src /app/src
COPY --chown=mcpuser:mcpuser fastmcp.json fastmcp-http.json ./

USER mcpuser

# Set HOME explicitly. Docker doesn't auto-derive HOME from /etc/passwd when
# a USER directive is set (moby/moby#2968), leaving HOME=/ at runtime. That
# made Path.home() resolve to "/" and ha-mcp tried to mkdir "/.ha-mcp" on
# every start — fatal under `read_only: true` (issue #1125).
ENV HOME=/home/mcpuser

# Activate virtual environment via PATH
ENV PATH="/app/.venv/bin:$PATH"

# Propagate dev build version into the runtime so startup logs / bug reports can
# surface e.g. '7.3.0.dev390' instead of the bare pyproject base version.
# Stable builds leave BUILD_VERSION unset; ha_mcp._version.get_version() then
# falls back to package metadata.
ARG BUILD_VERSION=""
ENV HA_MCP_BUILD_VERSION=${BUILD_VERSION}

# Environment variables (can be overridden)
ENV HOMEASSISTANT_URL="" \
    HOMEASSISTANT_TOKEN="" \
    BACKUP_HINT="normal"

# Default: Run in stdio mode using fastmcp.json
# For HTTP mode: docker run ... IMAGE ha-mcp-web
CMD ["fastmcp", "run", "fastmcp.json"]
