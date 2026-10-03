"""Base class for the search tool class and its mixins."""

from typing import Any


class SearchToolsBase:
    """Instance attributes set by ``SearchTools.__init__``."""

    _client: Any
    _smart_tools: Any
