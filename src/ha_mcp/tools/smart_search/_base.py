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
    def _extract_registry_list(result: Any, label: str) -> list[dict[str, Any]]:
        """Unwrap a WS registry-list result, returning ``[]`` on error/failure.

        Exceptions are logged at debug because every caller treats missing
        registry data as non-fatal rather than raising: the overview degrades
        its area enrichment, and the area search degrades to "no match found".
        """
        if isinstance(result, Exception):
            logger.debug(f"Could not fetch {label}: {result}")
            return []
        if isinstance(result, dict) and result.get("success"):
            registry: list[dict[str, Any]] = result.get("result", [])
            return registry
        return []
