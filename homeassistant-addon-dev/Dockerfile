# syntax=docker/dockerfile:1
# Home Assistant MCP Server Add-on (Dev Channel)
# Multi-stage build: uv for dependency resolution, slim Python for runtime
# WARNING: This is unstable and may break at any time

# --- Build stage: install dependencies with uv ---
FROM ghcr.io/astral-sh/uv:0.10.7-python3.13-trixie-slim@sha256:645c625876d01c77393338e2bccf4342ee2447680b3069f37004231fb97e2a62 AS builder

WORKDIR /app

# Compile bytecode for faster startup; copy mode required with cache mounts
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first (cached separately from source changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

# Copy source and install the project itself
COPY src/ ./src/

# Ensure bundled skills are present (fallback when submodule not initialized)
# When submodules: true is set in CI, COPY src/ already includes the pinned commit —
# skip extraction to preserve the submodule pin (zero-sync design)
ADD https://github.com/homeassistant-ai/skills/archive/main.tar.gz /tmp/skills.tar.gz
RUN if [ ! -d src/ha_mcp/resources/skills-vendor/skills ]; then \
    mkdir -p src/ha_mcp/resources/skills-vendor && \
    tar xzf /tmp/skills.tar.gz --strip-components=1 -C src/ha_mcp/resources/skills-vendor; \
    fi && rm -f /tmp/skills.tar.gz

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Runtime stage: clean image without uv ---
FROM python:3.13-slim@sha256:3de9a8d7aedbb7984dc18f2dff178a7850f16c1ae7c34ba9d7ecc23d0755e35f

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

CMD ["python3", "/start.py"]
