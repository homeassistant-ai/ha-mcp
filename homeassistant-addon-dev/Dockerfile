# syntax=docker/dockerfile:1
# Home Assistant MCP Server Add-on (Dev Channel)
# Multi-stage build: uv for dependency resolution, slim Python for runtime
# WARNING: This is unstable and may break at any time

# --- Build stage: install dependencies with uv ---
FROM ghcr.io/astral-sh/uv:0.12.7-python3.13-trixie-slim@sha256:6e00f3cc376554e74b6d39ce21aafd0b5b86e02eaaf4d0ff0fec1cc1032d2d25 AS builder

WORKDIR /app

# Compile bytecode for faster startup; copy mode required with cache mounts
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first (cached separately from source changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# Copy source and install the project itself
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Runtime stage: clean image without uv ---
FROM python:3.13-slim@sha256:881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2

WORKDIR /app

# Copy the virtual environment and startup script from builder
COPY --from=builder /app/.venv /app/.venv
COPY homeassistant-addon/start.py /

# Activate virtual environment via PATH
ENV PATH="/app/.venv/bin:$PATH"

# Labels
ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL \
    io.hass.name="Home Assistant MCP Server (Dev)" \
    io.hass.description="Development channel - AI assistant integration via MCP" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

# Propagate dev build version into the runtime so startup logs / bug reports can
# surface e.g. '7.3.0.dev229' instead of the bare pyproject base version.
# Stable addon builds leave BUILD_VERSION unset; ha_mcp._version.get_version()
# then falls back to package metadata.
ENV HA_MCP_BUILD_VERSION=${BUILD_VERSION}

CMD ["python3", "/start.py"]
