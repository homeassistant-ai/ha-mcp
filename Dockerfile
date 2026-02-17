# syntax=docker/dockerfile:1
# Home Assistant MCP Server - Production Docker Image
# Multi-stage build: uv for dependency resolution, slim Python for runtime
# Python 3.13 - Security support until 2029-10
# Base images pinned by digest - Renovate will create PRs for updates

# --- Build stage: install dependencies with uv ---
FROM ghcr.io/astral-sh/uv:0.9.30-python3.13-trixie-slim@sha256:a4174f4f3f3bc082b09e7c7d4de288c66b5a70c3cce22bbec35f23f0d24a8679 AS builder

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

# Create non-root user for security
RUN groupadd -r mcpuser && useradd -r -g mcpuser -m mcpuser

WORKDIR /app

# Copy the virtual environment, source, and config from builder
COPY --chown=mcpuser:mcpuser --from=builder /app/.venv /app/.venv
COPY --chown=mcpuser:mcpuser --from=builder /app/src /app/src
COPY --chown=mcpuser:mcpuser fastmcp.json fastmcp-http.json ./

USER mcpuser

# Activate virtual environment via PATH
ENV PATH="/app/.venv/bin:$PATH"

# Environment variables (can be overridden)
ENV HOMEASSISTANT_URL="" \
    HOMEASSISTANT_TOKEN="" \
    BACKUP_HINT="normal"

# Default: Run in stdio mode using fastmcp.json
# For HTTP mode: docker run ... IMAGE ha-mcp-web
CMD ["fastmcp", "run", "fastmcp.json"]
