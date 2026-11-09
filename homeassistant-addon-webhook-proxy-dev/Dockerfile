# Webhook Proxy for HA MCP
# Thin proxy — does NOT run an MCP server.
# Points at an existing ha-mcp addon (stable or dev) and exposes it
# via a Home Assistant webhook for remote access.

# PYTHON_BASE lets CI point the runtime base at another registry holding the
# same digest when ECR Public rate-limits the pull; the default is the pin.
ARG PYTHON_BASE=public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
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
