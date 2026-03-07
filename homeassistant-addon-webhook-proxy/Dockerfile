# syntax=docker/dockerfile:1
# Webhook Proxy for HA MCP
# Thin proxy — does NOT run an MCP server.
# Points at an existing ha-mcp addon (stable or dev) and exposes it
# via a Home Assistant webhook for remote access.

FROM python:3.13-slim@sha256:3de9a8d7aedbb7984dc18f2dff178a7850f16c1ae7c34ba9d7ecc23d0755e35f

COPY homeassistant-addon-webhook-proxy/start.py /
COPY homeassistant-addon-webhook-proxy/mcp_proxy /opt/mcp_proxy

ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL \
    io.hass.name="Nabu Casa / Webhook Proxy for HA MCP" \
    io.hass.description="Remote access proxy for HA MCP via webhook" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

CMD ["python3", "/start.py"]
