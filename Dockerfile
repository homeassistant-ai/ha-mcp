# Home Assistant MCP Server - Production Docker Image
# Uses uv for fast, reliable Python package management
# Python 3.13 - Security support until 2029-10
# uv version pinned - Dependabot will create PRs for updates

FROM ghcr.io/astral-sh/uv:0.9.13-python3.13-bookworm-slim

LABEL org.opencontainers.image.title="Home Assistant MCP Server" \
      org.opencontainers.image.description="AI assistant integration for Home Assistant via Model Context Protocol" \
      org.opencontainers.image.source="https://github.com/homeassistant-ai/ha-mcp" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.homeassistant-ai/ha-mcp"

WORKDIR /app

# Copy project files
COPY pyproject.toml ./
COPY src/ ./src/
COPY fastmcp.json fastmcp-http.json ./

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

# Default: Run in stdio mode using fastmcp.json
# For HTTP mode, override with: docker run ... ha-mcp fastmcp run fastmcp-http.json
ENTRYPOINT ["uv", "run", "--no-project"]
CMD ["fastmcp", "run", "fastmcp.json"]
