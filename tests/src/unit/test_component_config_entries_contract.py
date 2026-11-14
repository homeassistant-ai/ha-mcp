"""``ha_get_integration`` served by the component's ``config_entries`` command.

The tool runs on the server's own client against a real ``hass`` with the
``ha_mcp_tools/*`` commands registered; entries are ``MockConfigEntry`` rows.
Beyond the shape, three things hold: a single, list or subentry read sends none
of the legacy reads (the REST entry list, ``config_entries/get_single``, an
options flow started and aborted, ``config_entries/subentries/list``); a
resolved ``!secret`` in options is redacted; and the options are the stored
ones, so a field never set is absent, where an options flow's form would show
its default.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigFlowResult,
    ConfigSubentryDataWithId,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
from pytest_homeassistant.typing import MockHAClientWebSocket

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_config_entries import WS_CONFIG_ENTRIES
from ha_mcp.tools.tools_integrations import (
    IntegrationTools,
    fetch_entry_options_with_status,
)

from ._component_core import ws_result
from ._mock_flow_integration import options_only_config_flow


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The component-served client; ``config`` and ``logger`` answer the legacy
    reads and the log level."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "logger", {})
    return component_core_client


@pytest.fixture
def tools(client: HomeAssistantClient) -> IntegrationTools:
    return IntegrationTools(client)


def _entry(
    hass: HomeAssistant,
    entry_id: str = "cfg1",
    domain: str = "mqtt",
    *,
    options: dict[str, Any] | None = None,
    **kwargs: Any,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=domain,
        title=f"Title {entry_id}",
        options=options if options is not None else {"discovery": True},
        data={"password": "DATA_SECRET"},
        entry_id=entry_id,
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


@contextmanager
def _legacy_reads(client: HomeAssistantClient) -> Iterator[list[str]]:
    """The legacy reads the client makes meanwhile, by name."""
    made: list[str] = []
    with (
        patch.object(
            client, "get_config_entry", wraps=client.get_config_entry
        ) as single,
        patch.object(
            client, "start_options_flow", wraps=client.start_options_flow
        ) as flow,
        patch.object(
            client, "list_config_subentries", wraps=client.list_config_subentries
        ) as subentries,
        patch.object(
            client.httpx_client, "request", wraps=client.httpx_client.request
        ) as http,
    ):
        yield made
    for name, spy in (
        ("get_config_entry", single),
        ("start_options_flow", flow),
        ("list_config_subentries", subentries),
    ):
        made.extend([name] * spy.await_count)
    made.extend(
        "rest_entry_list"
        for call in http.await_args_list
        if "config_entries/entry" in str(call.args[1])
    )


async def test_a_single_entry_is_read_without_a_legacy_read(
    hass: HomeAssistant, client: HomeAssistantClient, tools: IntegrationTools
) -> None:
    _entry(hass)

    with _legacy_reads(client) as made:
        resp = await tools.ha_get_integration(entry_id="cfg1")

    assert resp["success"] is True
    entry = resp["entry"]
    assert (entry["entry_id"], entry["domain"], entry["state"]) == (
        "cfg1",
        "mqtt",
        "not_loaded",
    )
    assert entry["options"] == {"discovery": True}
    assert resp["log_level"] == "DEFAULT"
    # entry.data (credentials) never leaves the component.
    assert "DATA_SECRET" not in str(resp)
    assert made == []


async def test_subentries_are_their_identity_rows_without_data(
    hass: HomeAssistant, client: HomeAssistantClient, tools: IntegrationTools
) -> None:
    _entry(
        hass,
        subentries_data=[
            ConfigSubentryDataWithId(
                data={"k": "SUBSECRET"},
                subentry_id="sub1",
                subentry_type="device",
                title="Sub One",
                unique_id="u1",
            )
        ],
    )

    with _legacy_reads(client) as made:
        resp = await tools.ha_get_integration(entry_id="cfg1", include_subentries=True)

    assert resp["subentry_count"] == 1
    assert resp["subentries"] == [
        {
            "subentry_id": "sub1",
            "subentry_type": "device",
            "title": "Sub One",
            "unique_id": "u1",
        }
    ]
    assert "SUBSECRET" not in str(resp)
    # Lifted off the entry, not left nested in it.
    assert "subentries" not in resp["entry"]
    assert made == []


async def test_the_list_carries_each_entrys_options(
    hass: HomeAssistant, client: HomeAssistantClient, tools: IntegrationTools
) -> None:
    _entry(hass, "c1", "mqtt", options={"discovery": True})
    _entry(hass, "c2", "hue", options={"bridge": "x"})

    with _legacy_reads(client) as made:
        resp = await tools.ha_get_integration(include_options=True)

    by_id = {e["entry_id"]: e for e in resp["entries"]}
    assert by_id["c1"]["options"] == {"discovery": True}
    assert by_id["c2"]["options"] == {"bridge": "x"}
    assert resp["state_summary"] == {"not_loaded": 2}
    assert made == []


async def test_the_domain_filter_is_applied_by_the_component(
    hass: HomeAssistant, client: HomeAssistantClient, tools: IntegrationTools
) -> None:
    _entry(hass, "c1", "mqtt")
    _entry(hass, "c2", "hue")

    with _legacy_reads(client) as made:
        resp = await tools.ha_get_integration(domain="mqtt")

    assert [e["entry_id"] for e in resp["entries"]] == ["c1"]
    assert resp["domain_filter"] == "mqtt"
    assert made == []


async def test_a_resolved_secret_in_options_is_redacted(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    path = Path(hass.config.path("secrets.yaml"))
    await hass.async_add_executor_job(path.write_text, "api_password: sup3rsecret\n")
    _entry(hass, options={"password": "sup3rsecret", "keep": 1})

    resp = await tools.ha_get_integration(entry_id="cfg1")

    assert resp["entry"]["options"] == {"password": "**redacted**", "keep": 1}
    assert "sup3rsecret" not in str(resp)


_FLOW_DOMAIN = "options_shape_probe"


class _OptionsWithADefault(OptionsFlow):
    """An options form whose ``enabled`` field has a default."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        schema = probatio.Schema(
            {
                probatio.Optional("scan_interval"): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=3600)
                ),
                probatio.Optional("enabled", default=True): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )


async def test_options_are_the_stored_ones_not_the_forms_defaults(
    hass: HomeAssistant, client: HomeAssistantClient, tools: IntegrationTools
) -> None:
    """The tool documents this: a field never set is absent, where the options
    flow's form, which the legacy read uses, shows its default."""
    mock_integration(hass, MockModule(_FLOW_DOMAIN))
    mock_platform(hass, f"{_FLOW_DOMAIN}.config_flow")
    with mock_config_flow(_FLOW_DOMAIN, options_only_config_flow(_OptionsWithADefault)):
        entry = _entry(hass, domain=_FLOW_DOMAIN, options={"scan_interval": 30})

        resp = await tools.ha_get_integration(entry_id=entry.entry_id)
        from_form, read = await fetch_entry_options_with_status(client, entry.entry_id)

    assert resp["entry"]["options"] == {"scan_interval": 30}
    assert read is True
    assert from_form == {"scan_interval": 30, "enabled": True}


# Stored options with nested dicts (issue #2538).
_NESTED_OPTIONS = {
    # DurationSelector values; both carry the same inner keys.
    "no_data_grace": {"hours": 0, "minutes": 10, "seconds": 0},
    "retry_timeout": {"hours": 0, "minutes": 5, "seconds": 0},
    # An options-flow section holding DurationSelector values.
    "event_durations": {
        "emergency": {"hours": 1, "minutes": 0, "seconds": 0},
        "warning": {"hours": 0, "minutes": 15, "seconds": 0},
    },
    # A per-platform sub-dict (System Monitor stores its processes this way).
    "binary_sensor": {"process": ["python3"]},
    # A stored None, which the options-flow read skips.
    "fallback_group": None,
}


async def test_a_single_entry_returns_nested_options_where_they_are_stored(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    """None of a nested value is copied up to the top level (issue #2538)."""
    _entry(hass, options=_NESTED_OPTIONS)

    resp = await tools.ha_get_integration(entry_id="cfg1")

    assert resp["entry"]["options"] == _NESTED_OPTIONS


async def test_the_list_returns_nested_options_where_they_are_stored(
    hass: HomeAssistant, tools: IntegrationTools
) -> None:
    _entry(hass, "c1", "demo", options=_NESTED_OPTIONS)

    resp = await tools.ha_get_integration(include_options=True)

    assert resp["entries"][0]["options"] == _NESTED_OPTIONS


@pytest.mark.parametrize("domain", [None, "sun"])
async def test_the_type_filter_is_cores_own_match_without_the_rest_list(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    tools: IntegrationTools,
    domain: str | None,
) -> None:
    """``hue`` and ``deconz`` are hubs and ``sun`` a service in their manifests."""
    _entry(hass, "h1", "hue")
    _entry(hass, "s1", "sun", options={"elevation": 1})
    _entry(hass, "d1", "deconz")

    with _legacy_reads(client) as made:
        resp = await tools.ha_get_integration(
            domain=domain, integration_type="service", include_options=True
        )

    assert [e["entry_id"] for e in resp["entries"]] == ["s1"]
    assert resp["entries"][0]["options"] == {"elevation": 1}
    assert made == []


async def test_a_single_entry_read_does_not_need_cores_type_match(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``entry_id`` selects one entry, so a type sent beside it is not matched:
    the read works on a Core release without the private matcher."""
    _entry(hass, "h1", "hue")
    monkeypatch.delattr(
        "homeassistant.components.config.config_entries."
        "_async_matching_config_entries_json_fragments"
    )

    result = await ws_result(
        component_ws, WS_CONFIG_ENTRIES, entry_id="h1", type_filter=["service"]
    )

    assert [row["entry_id"] for row in result["entries"]] == ["h1"]
