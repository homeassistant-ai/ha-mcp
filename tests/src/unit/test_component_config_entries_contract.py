"""Cross-seam contract test for the ``config_entries`` capability.

Wires the REAL component ``_do_config_entries`` (plus its REAL async
``_config_entries_prep``, which loads the secret-scrub set off the loop)
underneath the mocked WS transport and drives the REAL ``ha_get_integration``
tool over it — so a vocabulary/shape drift on either side of the seam fails here
rather than shipping a component-served response the consumer mis-shapes. Mirrors
``test_component_readapi_contract.py`` (which owns the other commands and must not
be edited by parallel tasks); this file is the ``config_entries`` bridge.

It pins three things beyond shape parity: (1) the single/list/include_subentries
reads are served WITHOUT any OptionsFlow start/abort dance nor
subentries/list WS call; (2) the resolved-``!secret`` options scrub survives the
real prep end-to-end; (3) the options-shape caveat — the component path returns
RAW persisted options (a field never set is absent), which differs from the
OptionsFlow-derived shape that injects schema defaults.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import component_api, tools_integrations
from ha_mcp.tools.tools_integrations import options_from_form_flow

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import (
    FakeConfig,
    FakeConfigEntry,
    FakeHass,
    FakeSubentry,
    _FakeEnum,
    wsapi,
)
from .test_ha_get_integration_component_routing import (
    RoutingClient,
    _build_get_integration,
)


def _real_component_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose commands are served by the REAL component functions.

    ``info`` returns the real ``_do_info(hass)`` (so the caps probe sees the real
    capability list, config_entries included), and ``config_entries`` runs the
    real ``_config_entries_prep`` (secret load) then the real
    ``_do_config_entries`` against ``hass``.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info(hass)}
        assert command_type == wsapi.WS_CONFIG_ENTRIES, command_type
        params = dict(kwargs)
        extra = await wsapi._config_entries_prep(hass, params)
        return {"success": True, "result": wsapi._do_config_entries(hass, params, **extra)}

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _entry(
    entry_id: str = "cfg1",
    domain: str = "mqtt",
    *,
    options: dict[str, Any] | None = None,
    subentries: dict[str, Any] | None = None,
    **kw: Any,
) -> FakeConfigEntry:
    return FakeConfigEntry(
        domain,
        title=f"Title {entry_id}",
        options=options if options is not None else {"discovery": True},
        data={"password": "DATA_SECRET"},
        entry_id=entry_id,
        state=_FakeEnum("loaded"),
        source="user",
        supports_options=True,
        supports_unload=True,
        subentries=subentries or {},
        **kw,
    )


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()


def _no_dance(client: RoutingClient) -> None:
    """Assert the component read replaced every legacy round-trip."""
    assert client.start_options_flow_calls == 0
    assert client.abort_options_flow_calls == 0
    assert client.get_config_entry_calls == 0
    assert client.rest_list_calls == 0
    assert client.list_config_subentries_calls == 0


@pytest.mark.asyncio
async def test_single_entry_contract(tmp_path) -> None:
    """A real component single-entry read flows through the tool with the entry
    identity + options, and ZERO OptionsFlow dance."""
    hass = FakeHass(config_entries=[_entry("cfg1", options={"discovery": True})])
    hass.config = FakeConfig(tmp_path)
    ws = _real_component_ws(hass)
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1")

    assert resp["success"] is True
    assert resp["entry"]["entry_id"] == "cfg1"
    assert resp["entry"]["domain"] == "mqtt"
    assert resp["entry"]["state"] == "loaded"  # ConfigEntryState.value
    assert resp["entry"]["options"] == {"discovery": True}
    assert resp["log_level"] == "DEFAULT"
    # entry.data (credentials) never leaves the component.
    assert "DATA_SECRET" not in str(resp)
    _no_dance(client)
    assert ws.send_command.await_count >= 1


@pytest.mark.asyncio
async def test_single_entry_include_subentries_contract(tmp_path) -> None:
    """include_subentries surfaces the real component's subentry identity rows;
    the legacy subentries/list WS call never runs."""
    hass = FakeHass(
        config_entries=[
            _entry(
                "cfg1",
                subentries={
                    "sub1": FakeSubentry(
                        "sub1", "device", "Sub One", unique_id="u1",
                        data={"k": "SUBSECRET"},
                    )
                },
            )
        ]
    )
    hass.config = FakeConfig(tmp_path)
    ws = _real_component_ws(hass)
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1", include_subentries=True)

    assert resp["subentry_count"] == 1
    assert resp["subentries"] == [
        {
            "subentry_id": "sub1",
            "subentry_type": "device",
            "title": "Sub One",
            "unique_id": "u1",
        }
    ]
    # subentry.data is never emitted.
    assert "SUBSECRET" not in str(resp)
    # subentries were lifted off the entry itself, not left nested.
    assert "subentries" not in resp["entry"]
    _no_dance(client)


@pytest.mark.asyncio
async def test_list_contract(tmp_path) -> None:
    """A real component list read flows through the tool with per-row options and
    ZERO REST list / OptionsFlow probes."""
    hass = FakeHass(
        config_entries=[
            _entry("c1", domain="mqtt", options={"discovery": True}),
            _entry("c2", domain="hue", options={"bridge": "x"}),
        ]
    )
    hass.config = FakeConfig(tmp_path)
    ws = _real_component_ws(hass)
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(include_options=True)

    by_id = {e["entry_id"]: e for e in resp["entries"]}
    assert by_id["c1"]["options"] == {"discovery": True}
    assert by_id["c2"]["options"] == {"bridge": "x"}
    assert resp["state_summary"] == {"loaded": 2}
    _no_dance(client)


@pytest.mark.asyncio
async def test_list_domain_filter_contract(tmp_path) -> None:
    """The tool's domain filter is applied server-side by the real component."""
    hass = FakeHass(
        config_entries=[
            _entry("c1", domain="mqtt"),
            _entry("c2", domain="hue"),
        ]
    )
    hass.config = FakeConfig(tmp_path)
    ws = _real_component_ws(hass)
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(domain="mqtt")

    assert [e["entry_id"] for e in resp["entries"]] == ["c1"]
    assert resp["domain_filter"] == "mqtt"
    _no_dance(client)


@pytest.mark.asyncio
async def test_options_secret_scrub_end_to_end(tmp_path) -> None:
    """A resolved ``!secret`` in options is redacted through the real prep +
    _do_config_entries, end-to-end through the tool."""
    (tmp_path / "secrets.yaml").write_text(
        "api_password: sup3rsecret\n", encoding="utf-8"
    )
    hass = FakeHass(
        config_entries=[_entry("cfg1", options={"password": "sup3rsecret", "keep": 1})]
    )
    hass.config = FakeConfig(tmp_path)
    ws = _real_component_ws(hass)
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1")

    assert resp["entry"]["options"] == {"password": "**redacted**", "keep": 1}
    assert "sup3rsecret" not in str(resp)


@pytest.mark.asyncio
async def test_options_shape_caveat_raw_persisted(tmp_path) -> None:
    """Options-shape caveat: the component path returns RAW persisted options — a
    schema field never set is ABSENT — which differs from the OptionsFlow-derived
    shape that injects the field's default. This pins that the component path is
    the raw-persisted one (documented in the tool's OPTIONS note)."""
    # ``enabled`` has a schema default but was never persisted; only
    # ``scan_interval`` is stored.
    hass = FakeHass(config_entries=[_entry("cfg1", options={"scan_interval": 30})])
    hass.config = FakeConfig(tmp_path)
    ws = _real_component_ws(hass)
    client = RoutingClient()
    get_integration = _build_get_integration(client)

    with patch_ws(ws, tools_integrations):
        resp = await get_integration(entry_id="cfg1")

    component_options = resp["entry"]["options"]
    assert component_options == {"scan_interval": 30}
    assert "enabled" not in component_options  # never-set field is absent

    # The OptionsFlow-derived shape over the same entry WOULD inject the default
    # for the unset field — a different shape. This is the caveat, made explicit.
    flow_shape = options_from_form_flow(
        {
            "type": "form",
            "data_schema": [
                {"name": "scan_interval", "description": {"suggested_value": 30}},
                {"name": "enabled", "default": True},
            ],
        }
    )
    assert flow_shape == {"scan_interval": 30, "enabled": True}
    assert flow_shape != component_options
