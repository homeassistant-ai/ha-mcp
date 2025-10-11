# Home Assistant MCP Server - Production Docker Image
# Uses uv for fast, reliable Python package management
# Python 3.11 - Security support until 2027-10
# uv version pinned - Dependabot will create PRs for updates

FROM ghcr.io/astral-sh/uv:0.9.0-python3.11-bookworm-slim

LABEL org.opencontainers.image.title="Home Assistant MCP Server" \
      org.opencontainers.image.description="AI assistant integration for Home Assistant via Model Context Protocol" \
      org.opencontainers.image.source="https://github.com/homeassistant-ai/ha-mcp" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Copy project files
COPY pyproject.toml ./
COPY src/ ./src/

# Install dependencies and project with uv
# --no-cache: Don't cache downloaded packages
# --system: Install into system Python (not a virtual environment)
RUN uv pip install --system --no-cache .

# Create non-root user for security
RUN groupadd -r mcpuser && useradd -r -g mcpuser -m mcpuser && \
    chown -R mcpuser:mcpuser /app
USER mcpuser

# Environment variables (can be overridden)
ENV HOMEASSISTANT_URL="" \
    HOMEASSISTANT_TOKEN="" \
    BACKUP_HINT="normal"

# Default: Run in stdio mode (for MCP clients like Claude Desktop)
# Override CMD to run in streamable-http mode (for remote/web clients)
# Example: docker run ... ha-mcp python -c "from ha_mcp.__main__ import mcp; mcp.run(transport='streamable-http', host='0.0.0.0', port=8086)"
ENTRYPOINT ["uv", "run", "--no-project"]
CMD ["ha-mcp"]
