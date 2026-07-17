"""Cross-seam contract test for the ``services_list`` capability.

Like ``test_component_readapi_contract.py`` (which this file deliberately does
NOT edit — a parallel task's file), this wires the REAL component
``_do_services_list`` (driven through its REAL async ``_services_list_prep``)
underneath a mocked WS transport and then invokes the REAL
``ha_list_services`` tool, so a shape drift between the two sides of the
``services_list`` seam fails here rather than shipping a response the
consumer mis-shapes.

Parity pin: the consumer threads ``domain`` to the component (an exact match,
identical on both sides) but deliberately does NOT thread ``query`` — the
component's own ``query`` filter is a coarse SUPERSET that can drop a domain
from the payload ENTIRELY when nothing under it matches anywhere (name/
description/translation, including translation leaves the server's own exact
filter never inspects, e.g. a field-level string). ``_process_services``'s
``domains`` field is populated purely from ``domain_filter`` though,
independent of ``query_filter`` — so a query-trimmed component payload would
silently list fewer domains than legacy. This file's fixture constructs
exactly that would-be asymmetry (a domain that a coarse query pass would drop,
via a field-level translation hit) and asserts the ACTUAL (query-not-forwarded)
implementation still produces byte-identical output to the legacy REST + WS
translations fetch, ``domains`` included.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools import component_api
from ha_mcp.tools import tools_services as ts
from ha_mcp.tools.tools_services import register_services_tools

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import FakeHass, wsapi

# --- fixture catalog -------------------------------------------------------
# "cover" carries a field-level translation containing "warm" (only read by
# _process_service_fields in detail_level="full", never by the query filter),
# so the component's coarse per-domain scan (which inspects EVERY translation
# value regardless of nesting) keeps "cover" for query="warm" even though
# neither of its services' own name/description/top-level translation
# mentions "warm" — the server's exact per-entry filter then trims it to zero.
_DESCRIPTIONS = {
    "light": {
        "turn_on": {"description": "Turn a light on"},
        "turn_off": {"description": "Turn a light off"},
    },
    "cover": {
        "open_cover": {"description": "Open the cover"},
        "close_cover": {"description": "Close the cover"},
    },
}
_TRANSLATIONS = {
    "component.light.services.turn_on.name": "Turn on",
    "component.cover.services.open_cover.fields.tilt_position.description": (
        "Adjust to a warmly lit angle"
    ),
}


def _real_services_list_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose ``services_list`` frame runs the REAL prep + ``_do_*``."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        assert command_type == wsapi.WS_SERVICES_LIST
        params = dict(kwargs)
        extra = await wsapi._services_list_prep(hass, params)
        return {"success": True, "result": wsapi._do_services_list(hass, params, **extra)}

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


class RoutingClient:
    """Credentialed HA client spy: fails loudly if the legacy path is touched."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.legacy_rest_calls = 0
        self.legacy_ws_calls = 0

    async def get_services(self) -> list[dict[str, Any]]:
        self.legacy_rest_calls += 1
        # Byte-identical catalog to the component fixture, so a fallback (if
        # one incorrectly fired) would still produce the same output — the
        # call-count assertions are what actually pin routing.
        return [
            {"domain": domain, "services": dict(services)}
            for domain, services in _DESCRIPTIONS.items()
        ]

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "frontend/get_translations":
            self.legacy_ws_calls += 1
            return {"success": True, "result": {"resources": dict(_TRANSLATIONS)}}
        raise AssertionError(f"unexpected ws message {msg.get('type')!r}")


def _force_legacy_path(client: Any) -> None:
    """Pre-seed a negative caps cache entry so ``client`` skips straight to the
    legacy path with no real network probe (it is never wrapped in ``patch_ws``
    — it is the control side of each parity comparison)."""
    component_api._store_caps(client, None)


def _build_list_services(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_services_tools(mcp, client)
    return registered["ha_list_services"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.fixture
def _fake_descriptions_and_translations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed the REAL prep fixed descriptions/translations (no real hass needed)."""

    async def _fake_desc(hass: Any) -> dict[str, Any]:
        return _DESCRIPTIONS

    async def _fake_trans(hass: Any, language: str) -> dict[str, Any]:
        assert language == "en"
        return _TRANSLATIONS

    monkeypatch.setattr(wsapi, "_fetch_service_descriptions", _fake_desc)
    monkeypatch.setattr(wsapi, "_fetch_service_translations", _fake_trans)


@pytest.mark.asyncio
async def test_component_path_matches_legacy_no_filter(
    _fake_descriptions_and_translations: None,
) -> None:
    """No domain/query filter: component-routed output equals the legacy fetch."""
    ws = _real_services_list_ws(FakeHass())
    component_client = RoutingClient()
    legacy_client = RoutingClient()
    _force_legacy_path(legacy_client)

    with patch_ws(ws, ts):
        component_resp = await _build_list_services(component_client)()
    legacy_resp = await _build_list_services(legacy_client)()

    assert component_resp == legacy_resp
    assert set(component_resp["domains"]) == {"light", "cover"}
    assert component_client.legacy_rest_calls == 0
    assert component_client.legacy_ws_calls == 0
    assert legacy_client.legacy_rest_calls == 1
    assert legacy_client.legacy_ws_calls == 1


@pytest.mark.asyncio
async def test_query_that_would_coarse_drop_a_domain_still_parities(
    _fake_descriptions_and_translations: None,
) -> None:
    """"warm" here matches only a field-level translation under "cover". If
    ``query`` were forwarded to the component, its coarse per-domain scan
    (which reads every translation value, field-level included) would KEEP
    "cover" — but drop "light" entirely (zero matches anywhere in it), while
    legacy's ``domains`` key lists every domain passing ``domain_filter``
    regardless of query. Forwarding would therefore silently narrow
    ``domains`` versus legacy. Since the consumer never forwards ``query``,
    the full (domain-scoped-only) catalog reaches ``_process_services`` either
    way, so ``domains`` — and every other key — stays byte-identical to the
    legacy fetch: both trim "cover"'s services to zero (its name/description/
    top-level translation never mention "warm") while still listing both
    "light" and "cover" in ``domains``.
    """
    ws = _real_services_list_ws(FakeHass())
    component_client = RoutingClient()
    legacy_client = RoutingClient()
    _force_legacy_path(legacy_client)

    with patch_ws(ws, ts):
        component_resp = await _build_list_services(component_client)(query="warm")
    legacy_resp = await _build_list_services(legacy_client)(query="warm")

    assert component_resp == legacy_resp
    assert set(component_resp["domains"]) == {"light", "cover"}
    assert component_resp["services"] == {}
    assert component_resp["total_count"] == 0


@pytest.mark.asyncio
async def test_domain_filter_and_pagination_parity(
    _fake_descriptions_and_translations: None,
) -> None:
    """domain filter + limit/offset pagination produce byte-identical pages."""
    ws = _real_services_list_ws(FakeHass())
    component_client = RoutingClient()
    legacy_client = RoutingClient()
    _force_legacy_path(legacy_client)

    with patch_ws(ws, ts):
        component_resp = await _build_list_services(component_client)(
            domain="light", limit=1, offset=1
        )
    legacy_resp = await _build_list_services(legacy_client)(
        domain="light", limit=1, offset=1
    )

    assert component_resp == legacy_resp
    assert component_resp["total_count"] == 2
    assert component_resp["count"] == 1
    assert component_resp["has_more"] is False


@pytest.mark.asyncio
async def test_translation_sourced_name_lookup_identical_both_paths(
    _fake_descriptions_and_translations: None,
) -> None:
    """The (no-op, by construction) translation-name lookup behaves identically
    from either source: ``_build_service_entry`` keys the lookup by the bare
    ``component.<domain>.services.<service>`` prefix, which never matches the
    flat ``...name``-suffixed keys either path supplies, so both fall back to
    the title-cased service name — pinned equal rather than assumed."""
    ws = _real_services_list_ws(FakeHass())
    component_client = RoutingClient()
    legacy_client = RoutingClient()
    _force_legacy_path(legacy_client)

    with patch_ws(ws, ts):
        component_resp = await _build_list_services(component_client)(domain="light")
    legacy_resp = await _build_list_services(legacy_client)(domain="light")

    assert (
        component_resp["services"]["light.turn_on"]["name"]
        == legacy_resp["services"]["light.turn_on"]["name"]
        == "Turn On"
    )
