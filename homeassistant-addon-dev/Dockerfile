# Home Assistant MCP Server Add-on (Dev Channel)
# Pulls from :dev Docker tag for latest development builds
# WARNING: This is unstable and may break at any time

FROM ghcr.io/astral-sh/uv:python3.13-bookworm

WORKDIR /app

# Copy project files from project root
COPY pyproject.toml ./
COPY src ./src

# Install dependencies and project with uv
RUN uv pip install --system --no-cache .

# Copy Python startup script (shared with stable)
COPY homeassistant-addon/start.py /
RUN chmod a+x /start.py

# Labels
LABEL \
    io.hass.name="Home Assistant MCP Server (Dev)" \
    io.hass.description="Development channel - AI assistant integration via MCP" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

CMD ["python3", "/start.py"]
