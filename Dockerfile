# Home Assistant MCP Server - Production Docker Image
# Standalone deployment without uv dependency

FROM python:3.11-slim

LABEL org.opencontainers.image.title="Home Assistant MCP Server" \
      org.opencontainers.image.description="AI assistant integration for Home Assistant via Model Context Protocol" \
      org.opencontainers.image.source="https://github.com/homeassistant-ai/ha-mcp" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install with pip
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY pyproject.toml README.md LICENSE ./

# Install package in editable mode
RUN pip install --no-cache-dir -e .

# Create non-root user for security
RUN groupadd -r mcpuser && useradd -r -g mcpuser mcpuser && \
    chown -R mcpuser:mcpuser /app
USER mcpuser

# Environment variables (can be overridden)
ENV HOMEASSISTANT_URL="" \
    HOMEASSISTANT_TOKEN="" \
    BACKUP_HINT="normal"

# Expose port for MCP server (if needed in future)
EXPOSE 8080

# Run the MCP server
CMD ["ha-mcp"]
