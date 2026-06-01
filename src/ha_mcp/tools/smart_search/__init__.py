"""Smart search tools for Home Assistant MCP server.

The implementation is split across sibling modules to keep each unit
focused (see issue #925):

- ``_search_config``: shared constants and pure helpers
- ``_search_base``: attributes shared across mixins + registry-list helper
- ``_search_deep``: ``deep_search`` (config-definition search)
- ``_search_overview``: ``get_system_overview``
- ``_search_entities``: ``smart_entity_search`` + ``get_entities_by_area``
"""

import logging

from ...client.rest_client import HomeAssistantClient
from ...config import get_global_settings
from ...utils.fuzzy_search import create_fuzzy_searcher

# Re-export shared constants so existing ``smart_search.<CONST>`` references
# keep resolving.
from ._config import (
    AUTOMATION_CONFIG_TIME_BUDGET,
    BULK_REST_TIMEOUT,
    BULK_WEBSOCKET_TIMEOUT,
    DEFAULT_CONCURRENCY_LIMIT,
    INDIVIDUAL_CONFIG_TIMEOUT,
    INDIVIDUAL_FETCH_BATCH_SIZE,
    SCENE_CONFIG_TIME_BUDGET,
    SCRIPT_CONFIG_TIME_BUDGET,
)
from ._deep import DeepSearchMixin
from ._entities import EntitySearchMixin
from ._overview import SystemOverviewMixin

logger = logging.getLogger(__name__)


class SmartSearchTools(DeepSearchMixin, SystemOverviewMixin, EntitySearchMixin):
    """Smart search tools with fuzzy matching and AI optimization."""

    def __init__(
        self, client: HomeAssistantClient | None = None, fuzzy_threshold: int = 60
    ):
        """Initialize with Home Assistant client."""
        # Always load settings for configuration access
        self.settings = get_global_settings()

        # Use provided client or create new one
        if client is None:
            self.client = HomeAssistantClient()
            fuzzy_threshold = self.settings.fuzzy_threshold
        else:
            self.client = client

        self.fuzzy_searcher = create_fuzzy_searcher(threshold=fuzzy_threshold)


def create_smart_search_tools(
    client: HomeAssistantClient | None = None,
) -> SmartSearchTools:
    """Create smart search tools instance."""
    return SmartSearchTools(client)
