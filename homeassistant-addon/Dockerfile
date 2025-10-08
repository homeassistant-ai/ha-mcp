ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install required build tools
RUN apk add --no-cache \
    gcc \
    g++ \
    musl-dev \
    libffi-dev \
    openssl-dev

# Set up working directory
WORKDIR /app

# Copy requirements from parent and install dependencies
COPY ../requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy source code from parent directory
COPY ../src ./src
COPY ../pyproject.toml ../README.md ../LICENSE ./

# Install the package
RUN pip3 install --no-cache-dir -e .

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
