# syntax=docker/dockerfile:1
# Home Assistant MCP Server - Production Docker Image
# Multi-stage build: uv for dependency resolution, slim Python for runtime
# Python 3.13 - Security support until 2029-10
# Base images pinned by digest - Renovate will create PRs for updates

# --- Build stage: install dependencies with uv ---
FROM ghcr.io/astral-sh/uv:0.12.20-python3.13-trixie-slim@sha256:520ead6668d302a3d307d63e3b2b02a3205e69f503dfc1af836d609d3ff591e0 AS builder

WORKDIR /app

# Compile bytecode for faster startup; copy mode required with cache mounts
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first (cached separately from source changes). The
# dev-only test-env pyproject is copied because --locked reads every
# package in the lock file, including dev ones that are not installed.
COPY pyproject.toml uv.lock ./
COPY tests/test-env/pyproject.toml ./tests/test-env/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# Copy source and config, then install the project itself
COPY README.md LICENSE ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- Runtime stage: clean image without uv ---
FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

LABEL org.opencontainers.image.title="Home Assistant MCP Server" \
      org.opencontainers.image.description="AI assistant integration for Home Assistant via Model Context Protocol" \
      org.opencontainers.image.source="https://github.com/homeassistant-ai/ha-mcp" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.homeassistant-ai/ha-mcp"

# Create non-root user. /home/mcpuser is mode 0755 (not the default 0700) so
# that callers running with `--user UID:GID` overrides — common in hardened
# Docker setups, see issue #1125 — can stat HOME-relative paths. Write
# access stays restricted to mcpuser via ownership.
#
# ~/.ha-mcp is pre-created and owned by mcpuser because Docker initializes a
# fresh named volume from the image's directory at the mount point, ownership
# included. Without this the mount point wouldn't exist in the image, Docker
# would create it root-owned, and the documented
# `-v ha-mcp-data:/home/mcpuser/.ha-mcp` would leave the container unable to
# write there. ha-mcp then warns and falls back to a tmpdir (see
# utils/data_paths.py), losing settings on every restart — issue #2078.
#
# The UID/GID are pinned rather than left to `-r`'s dynamic allocation: a
# volume records numeric ownership, not names. If a base-image update shifted
# the IDs `groupadd -r`/`useradd -r` hand out, an existing ha-mcp-data volume
# would stay owned by the old UID and the new process couldn't write to it —
# reintroducing exactly the tmpdir fallback this change exists to prevent.
# 999 is what `-r` already allocated on this base image, so pinning it changed
# nothing for existing deployments: volumes and `chown 999:999` bind mounts
# created before the pin keep working.
# Bind-mounting a host directory instead? It must be writable by UID 999 —
# or by whatever UID you pass to `--user`, which a 999-owned named volume
# will NOT satisfy.
RUN groupadd -r -g 999 mcpuser \
    && useradd -r -u 999 -g mcpuser -m mcpuser \
    && chmod 0755 /home/mcpuser \
    && mkdir -p /home/mcpuser/.ha-mcp \
    && chown mcpuser:mcpuser /home/mcpuser/.ha-mcp

WORKDIR /app

# Copy the virtual environment and source from builder
COPY --chown=mcpuser:mcpuser --from=builder /app/.venv /app/.venv
COPY --chown=mcpuser:mcpuser --from=builder /app/src /app/src

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

# Default: Run in stdio mode
# For HTTP mode: docker run ... IMAGE ha-mcp-web
CMD ["ha-mcp"]
