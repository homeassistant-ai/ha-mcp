ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install uv and required build tools
RUN apk add --no-cache \
    curl \
    gcc \
    g++ \
    musl-dev \
    libffi-dev \
    openssl-dev \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.cargo/bin/uv /usr/local/bin/uv

# Set up working directory
WORKDIR /app

# Copy project files from parent directory
COPY ../pyproject.toml ./
COPY ../src ./src

# Install dependencies and project with uv
RUN uv pip install --system --no-cache .

# Copy run script
COPY run.sh /
RUN chmod a+x /run.sh

# Labels
LABEL \
    io.hass.name="Home Assistant MCP Server" \
    io.hass.description="AI assistant integration via Model Context Protocol" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

CMD [ "/run.sh" ]
