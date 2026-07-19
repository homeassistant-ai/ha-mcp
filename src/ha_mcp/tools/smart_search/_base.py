"""Shared base for the smart-search feature mixins.

``_SearchBase`` declares the instance attributes that
``SmartSearchTools.__init__`` sets, so each feature mixin type-checks in
isolation, and hosts the registry-list helper shared by the overview and
entity/area search mixins.
"""

import logging
from typing import Any

from ...client.rest_client import HomeAssistantClient

logger = logging.getLogger(__name__)


class _SearchBase:
    """Attributes set by ``SmartSearchTools.__init__`` plus shared helpers."""

    client: HomeAssistantClient
    fuzzy_searcher: Any
    settings: Any

    @staticmethod
    def _extract_registry_list(
        result: Any, label: str, warnings: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Unwrap a WS registry-list result, returning ``[]`` on error/failure.

        Every caller treats missing registry data as non-fatal rather than
        raising: the overview degrades its area enrichment, and the area
        search degrades to "no match found". Degrading is fine; degrading
        silently is not — a caller that passes ``warnings`` gets a line
        naming what was unavailable and why, so a thinner answer is
        distinguishable from a genuinely empty registry (#1947).
        """
        cause: str | None = None
        registry: list[dict[str, Any]] = []
        if isinstance(result, Exception):
            logger.debug(f"Could not fetch {label}: {result}")
            cause = str(result) or type(result).__name__
        elif isinstance(result, dict) and result.get("success"):
            registry = result.get("result", [])
        elif isinstance(result, dict):
            cause = str(result.get("error") or "request failed")
        else:
            cause = f"unexpected response type: {type(result).__name__}"

        if cause is not None and warnings is not None:
            warnings.append(f"{label} unavailable: {cause}")
        return registry
