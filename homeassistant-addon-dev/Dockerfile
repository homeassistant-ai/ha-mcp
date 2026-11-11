# Home Assistant MCP Server Add-on (Dev Channel)
# Multi-stage build: uv for dependency resolution, slim Python for runtime
# WARNING: This is unstable and may break at any time

# PYTHON_BASE lets CI point the runtime base at another registry holding the
# same digest when ECR Public rate-limits the pull; the default is the pin.
ARG PYTHON_BASE=public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

# --- Build stage: install dependencies with uv ---
FROM ghcr.io/astral-sh/uv:0.13.7-python3.14-trixie-slim@sha256:e80c05a310400cfc90af00b5b7271ef9ad24816e3265e1162af8a678e1aad099 AS builder

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

# Copy source and install the project itself
COPY README.md LICENSE ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Runtime stage: clean image without uv ---
FROM ${PYTHON_BASE}

WORKDIR /app

# Copy the virtual environment and startup script from builder
COPY --from=builder /app/.venv /app/.venv
COPY homeassistant-addon/start.py /
COPY homeassistant-addon/app_options.json /

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
