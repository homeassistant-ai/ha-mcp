"""Inline resource URLs in the legacy worker form the retired writer produced."""

from __future__ import annotations

import base64

from ha_mcp.tools.resource_codec import LEGACY_WORKER_BASE_URL


def legacy_url(content: str, resource_type: str = "module") -> str:
    """Build a legacy worker inline URL the way the retired writer did."""
    encoded = base64.urlsafe_b64encode(content.encode()).decode()
    return f"{LEGACY_WORKER_BASE_URL}/{encoded}?type={resource_type}"
