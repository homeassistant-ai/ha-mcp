"""Cancellation must propagate out of deep-search's per-item fan-outs.

The three gathers in ``_deep.py`` run with ``return_exceptions=True``, so a
``CancelledError`` raised inside a child comes back as a *value*. The result
loops used to branch ``isinstance(result, tuple)`` / ``elif isinstance(result,
Exception)`` with nothing below — a ``BaseException`` matched neither arm and
was silently discarded, so a cancelled search reported itself as one that ran
and found nothing. Each loop now re-raises non-``Exception`` results, the same
line the per-item fan-outs in ``tools_entities`` and ``tools_search`` draw.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools


def _make_tools() -> SmartSearchTools:
    client = MagicMock()
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


async def test_helper_type_cancellation_propagates():
    """A cancelled input_* list fetch must not become a silent no-result.

    Six helper types are gathered; the second one's task is cancelled. Before
    the guard, the ``CancelledError`` matched neither loop arm and the search
    returned normally with the cancelled type simply missing.
    """
    tools = _make_tools()
    tools._search_helper_type = AsyncMock(
        side_effect=[
            ([], False),
            asyncio.CancelledError(),
            ([], False),
            ([], False),
            ([], False),
            ([], False),
        ]
    )

    with pytest.raises(asyncio.CancelledError):
        await tools._deep_search_helpers("query", False, asyncio.Semaphore(5), False)


async def test_dashboard_cancellation_propagates():
    """A cancelled per-dashboard fetch must not be dropped from the count.

    The enclosing ``except Exception`` re-raise does not catch it either —
    ``CancelledError`` is a ``BaseException`` — so it reaches the caller.
    """
    tools = _make_tools()
    tools._search_one_dashboard = AsyncMock(
        side_effect=[([], False), asyncio.CancelledError()]
    )

    with (
        patch(
            "ha_mcp.tools.smart_search._deep.fetch_dashboards_list",
            AsyncMock(return_value=[{"url_path": "dash-a", "title": "A"}]),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await tools._deep_search_dashboards("query", False, asyncio.Semaphore(5))


async def test_flow_helper_cancellation_propagates():
    """A cancelled options-flow probe must not be logged away as a code bug.

    The old loop's ``elif isinstance(item, Exception)`` arm was written for
    scoring bugs; a ``CancelledError`` matched nothing and vanished.
    """
    tools = _make_tools()
    tools.client._request = AsyncMock(
        return_value=[{"entry_id": "1"}, {"entry_id": "2"}]
    )
    tools._is_flow_helper_entry = lambda _entry: True
    tools._score_flow_entry = AsyncMock(
        side_effect=[(None, False), asyncio.CancelledError()]
    )

    with pytest.raises(asyncio.CancelledError):
        await tools._search_flow_helpers(
            "query", False, asyncio.Semaphore(5), include_config=False
        )
