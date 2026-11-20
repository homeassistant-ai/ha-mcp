"""Cancellation must propagate out of deep-search's per-item fan-outs.

The three gathers in ``_deep.py`` run with ``return_exceptions=True``, so a
``CancelledError`` raised inside a child comes back as a *value*. The result
loops used to branch ``isinstance(result, tuple)`` / ``elif isinstance(result,
Exception)`` with nothing below — a ``BaseException`` matched neither arm and
was silently discarded, so a cancelled search reported itself as one that ran
and found nothing. Each loop now re-raises non-``Exception`` results, the same
line the per-item fan-outs in ``tools_entities`` and ``tools_search`` draw.

Each test runs the public ``deep_search`` on Core's own data and cancels one
child of the fan-out, which is this repository's own method.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools


def _cancel_second(real_method):
    calls = 0

    async def call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return await real_method(*args, **kwargs)

    return AsyncMock(side_effect=call)


async def test_helper_type_cancellation_propagates(
    core_client: HomeAssistantClient,
) -> None:
    """A cancelled input_* list fetch must not become a silent no-result.

    The second helper type's task is cancelled. Before the guard, the
    ``CancelledError`` matched neither loop arm and the search returned
    normally with the cancelled type simply missing.
    """
    tools = SmartSearchTools(client=core_client)
    one_cancelled = _cancel_second(tools.search_helper_type)

    with (
        patch.object(tools, "search_helper_type", one_cancelled),
        pytest.raises(asyncio.CancelledError),
    ):
        await tools.deep_search("query", search_types=["helper"])

    assert one_cancelled.await_count >= 2


async def test_dashboard_cancellation_propagates(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A cancelled per-dashboard fetch must not be dropped from the count.

    The enclosing ``except Exception`` re-raise does not catch it either —
    ``CancelledError`` is a ``BaseException`` — so it reaches the caller.
    """
    assert await async_setup_component(hass, "lovelace", {})
    created = await core_client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": "dash-a",
            "title": "A",
            "mode": "storage",
        }
    )
    assert created["success"] is True
    tools = SmartSearchTools(client=core_client)
    one_cancelled = _cancel_second(tools.search_one_dashboard)

    with (
        patch.object(tools, "search_one_dashboard", one_cancelled),
        pytest.raises(asyncio.CancelledError),
    ):
        await tools.deep_search("query", search_types=["dashboard"])

    assert one_cancelled.await_count >= 2


async def test_flow_helper_cancellation_propagates(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A cancelled options-flow probe must not be logged away as a code bug.

    The old loop's ``elif isinstance(item, Exception)`` arm was written for
    scoring bugs; a ``CancelledError`` matched nothing and vanished.
    """
    assert await async_setup_component(hass, "config", {})
    for domain in ("template", "group"):
        MockConfigEntry(domain=domain, title=f"{domain} helper").add_to_hass(hass)
    tools = SmartSearchTools(client=core_client)
    one_cancelled = _cancel_second(tools.score_flow_entry)

    with (
        patch.object(tools, "score_flow_entry", one_cancelled),
        pytest.raises(asyncio.CancelledError),
    ):
        await tools.deep_search("query", search_types=["helper"])

    assert one_cancelled.await_count >= 2
