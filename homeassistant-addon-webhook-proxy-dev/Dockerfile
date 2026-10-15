# Webhook Proxy for HA MCP
# Thin proxy — does NOT run an MCP server.
# Points at an existing ha-mcp addon (stable or dev) and exposes it
# via a Home Assistant webhook for remote access.

# PYTHON_BASE lets CI point the runtime base at another registry holding the
# same digest when ECR Public rate-limits the pull; the default is the pin.
ARG PYTHON_BASE=public.ecr.aws/docker/library/python:3.13-slim@sha256:3de9a8d7aedbb7984dc18f2dff178a7850f16c1ae7c34ba9d7ecc23d0755e35f
FROM ${PYTHON_BASE}

COPY start.py /
COPY mcp_proxy_dev /opt/mcp_proxy_dev

ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL \
    io.hass.name="Nabu Casa - Webhook Proxy for HA MCP (Dev)" \
    io.hass.description="DEV channel — remote access proxy for HA MCP via webhook" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

CMD ["python3", "/start.py"]
