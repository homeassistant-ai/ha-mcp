"""ha_get_integration's diagnostics parameters and the options-flow probe.

The tools run on the server's own client against a real ``hass``. Core's
``diagnostics`` integration serves a test integration's dumps, and options
flows are the integration's own, opened and aborted by Core's flow manager.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import Mock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.integration_schema import fetch_options_schema
from ha_mcp.tools.tools_integrations import (
    IntegrationTools,
    fetch_entry_options_with_status,
)

from ._mock_flow_integration import mock_flow_integration, options_only_config_flow
from ._tool_errors import rejected_call_body

DUMPED = "dumped_entry"
PROBED = "probed_options"


class Dumped(NamedTuple):
    entry: MockConfigEntry
    entry_dump: dict[str, Any]
    device_dump: dict[str, Any]


@pytest.fixture
async def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> IntegrationTools:
    # ``config`` serves the entry reads, ``logger`` the entry's log level.
    for domain in ("config", "logger"):
        assert await async_setup_component(hass, domain, {})
    return IntegrationTools(core_client)


@pytest.fixture
async def dumped(hass: HomeAssistant, tools: IntegrationTools) -> Dumped:
    """An entry whose integration has a diagnostics platform, beside Core's
    ``diagnostics``; its entry and device dumps return what the test puts in."""
    entry_dump: dict[str, Any] = {}
    device_dump: dict[str, Any] = {}

    async def dump_entry(core: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
        return entry_dump

    async def dump_device(
        core: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
    ) -> dict[str, Any]:
        return device_dump

    mock_integration(hass, MockModule(DUMPED))
    mock_platform(
        hass,
        f"{DUMPED}.diagnostics",
        Mock(
            async_get_config_entry_diagnostics=dump_entry,
            async_get_device_diagnostics=dump_device,
        ),
    )
    assert await async_setup_component(hass, DUMPED, {})
    assert await async_setup_component(hass, "diagnostics", {})
    entry = MockConfigEntry(domain=DUMPED)
    entry.add_to_hass(hass)
    return Dumped(entry, entry_dump, device_dump)


# --- diagnostics -------------------------------------------------------------------


async def test_include_diagnostics_attaches_the_entrys_dump(
    tools: IntegrationTools, dumped: Dumped
) -> None:
    dumped.entry_dump["bridge"] = {"ip": "10.0.0.5"}

    result = await tools.ha_get_integration(
        entry_id=dumped.entry.entry_id, include_diagnostics=True
    )

    assert result["diagnostics"]["config_entry_id"] == dumped.entry.entry_id
    assert result["diagnostics"]["data"]["data"] == {"bridge": {"ip": "10.0.0.5"}}
    assert "warnings" not in result


async def test_device_id_returns_that_devices_dump(
    tools: IntegrationTools, dumped: Dumped, device_registry: dr.DeviceRegistry
) -> None:
    dumped.entry_dump["scope"] = "entry"
    dumped.device_dump["scope"] = "device"
    device = device_registry.async_get_or_create(
        config_entry_id=dumped.entry.entry_id, identifiers={(DUMPED, "dev")}
    )

    result = await tools.ha_get_integration(
        entry_id=dumped.entry.entry_id, include_diagnostics=True, device_id=device.id
    )

    assert result["diagnostics"]["device_id"] == device.id
    assert result["diagnostics"]["data"]["data"] == {"scope": "device"}


async def test_an_entry_read_without_include_diagnostics_fetches_no_dump(
    tools: IntegrationTools, dumped: Dumped
) -> None:
    result = await tools.ha_get_integration(entry_id=dumped.entry.entry_id)

    assert "diagnostics" not in result
    assert "warnings" not in result


async def test_device_id_without_include_diagnostics_is_ignored_with_a_warning(
    tools: IntegrationTools, dumped: Dumped
) -> None:
    result = await tools.ha_get_integration(
        entry_id=dumped.entry.entry_id, device_id="dev_xyz"
    )

    assert "diagnostics" not in result
    assert any("device_id" in w and "ignored" in w for w in result["warnings"])


async def test_diagnostics_fields_and_byte_cap_both_reach_the_dump(
    tools: IntegrationTools, dumped: Dumped
) -> None:
    """The cap fires on what the fields kept, so the keys it names are only
    the kept ones."""
    result = await tools.ha_get_integration(
        entry_id=dumped.entry.entry_id,
        include_diagnostics=True,
        diagnostics_fields='["data", "integration_manifest", "absent"]',
        diagnostics_truncate_at_bytes=1,
    )

    diagnostics = result["diagnostics"]
    assert diagnostics["truncated"] is True
    assert diagnostics["available_fields"] == ["data", "integration_manifest"]
    assert diagnostics["omitted_fields"] == ["absent"]


async def test_diagnostics_data_path_pages_a_list_in_the_dump(
    tools: IntegrationTools, dumped: Dumped
) -> None:
    dumped.entry_dump["devices"] = [{"n": n} for n in range(15)]

    result = await tools.ha_get_integration(
        entry_id=dumped.entry.entry_id,
        include_diagnostics=True,
        diagnostics_data_path="data.devices",
        diagnostics_data_offset=5,
        diagnostics_data_limit=10,
    )

    page = result["diagnostics"]["data"]
    assert page["items"] == [{"n": n} for n in range(5, 15)]
    assert (page["offset"], page["limit"], page["total"]) == (5, 10, 15)


@pytest.mark.parametrize(
    ("call", "named"),
    [
        ({"include_diagnostics": True, "device_id": "dev_xyz"}, "device_id"),
        ({"diagnostics_fields": ["home_assistant"]}, "diagnostics_fields"),
        (
            {"diagnostics_data_path": "data.devices", "diagnostics_data_limit": 10},
            "diagnostics_data_path",
        ),
        ({"diagnostics_data_offset": 5}, "diagnostics_data_offset"),
    ],
    ids=["include_and_device", "fields", "data_path", "offset_alone"],
)
async def test_list_mode_names_each_ignored_diagnostics_parameter(
    tools: IntegrationTools, dumped: Dumped, call: dict[str, Any], named: str
) -> None:
    """Without entry_id the call lists entries; a diagnostics argument the
    list cannot use is named, not silently dropped."""
    result = await tools.ha_get_integration(**call)

    assert [row["entry_id"] for row in result["entries"]] == [dumped.entry.entry_id]
    warning = next(w for w in result["warnings"] if "entry_id was not set" in w)
    assert named in warning


async def test_a_non_string_data_path_is_refused_as_an_invalid_parameter(
    tools: IntegrationTools,
) -> None:
    data_path: Any = {"not": "a string"}

    body = await rejected_call_body(
        tools.ha_get_integration(
            entry_id="entry_abc",
            include_diagnostics=True,
            diagnostics_data_path=data_path,
        )
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "diagnostics_data_path" in body["error"]["message"]


# --- the options-flow probe ---------------------------------------------------------


def _form_flow(schema: vol.Schema) -> type[OptionsFlow]:
    """An options flow whose first step shows ``schema`` with the entry's
    stored options as suggested values, as Core's helper flows do."""

    class ShowsForm(OptionsFlow):
        async def async_step_init(
            self, user_input: dict[str, Any] | None = None
        ) -> ConfigFlowResult:
            return self.async_show_form(
                step_id="init",
                data_schema=self.add_suggested_values_to_schema(
                    schema, self.config_entry.options
                ),
            )

    return ShowsForm


class _MenuFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["basic"])


ProbeEntry = Callable[[type[OptionsFlow] | None, dict[str, Any]], MockConfigEntry]


@pytest.fixture
def probe_entry(
    hass: HomeAssistant,
    tools: IntegrationTools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ProbeEntry]:
    """Makes an entry holding the given options, whose integration's options
    flow is the given one (None: it has no options flow)."""
    mock_flow_integration(hass, PROBED, tmp_path, monkeypatch)
    with ExitStack() as stack:

        def make(
            flow: type[OptionsFlow] | None, options: dict[str, Any]
        ) -> MockConfigEntry:
            handler = (
                options_only_config_flow(flow)
                if flow
                else type("NoOptions", (ConfigFlow,), {"VERSION": 1})
            )
            stack.enter_context(mock_config_flow(PROBED, handler))
            entry = MockConfigEntry(domain=PROBED, options=options)
            entry.add_to_hass(hass)
            return entry

        yield make


def _section(fields: dict[Any, Any]) -> section:
    return section(vol.Schema(fields), {"collapsed": True})


@pytest.mark.parametrize(
    ("fields", "stored", "read"),
    [
        ({vol.Optional("max_value", default=42): int}, {}, {"max_value": 42}),
        ({vol.Required("fixed"): "constant"}, {}, {"fixed": "constant"}),
        (
            {vol.Required("state"): selector.TemplateSelector()},
            {"state": "{{ states('sensor.x') | float }}"},
            {"state": "{{ states('sensor.x') | float }}"},
        ),
        (
            {
                vol.Required("state"): str,
                vol.Required("advanced_options"): _section(
                    {vol.Optional("availability"): str}
                ),
            },
            {"state": "{{ 1 }}", "advanced_options": {"availability": "{{ 2 }}"}},
            {"state": "{{ 1 }}", "availability": "{{ 2 }}"},
        ),
        (
            {vol.Required("hide_members", default=False): bool},
            {"hide_members": True},
            {"hide_members": True},
        ),
        (
            {vol.Required("field", default=True): bool},
            {"field": False},
            {"field": False},
        ),
        (
            {vol.Optional("field", default="fallback"): vol.Any(None, str)},
            {"field": None},
            {"field": "fallback"},
        ),
        ({vol.Required("field"): "constant"}, {"field": "stored"}, {"field": "stored"}),
        (
            {
                vol.Optional("optional_blank"): str,
                vol.Optional("explicit_none", default=None): vol.Any(None, str),
            },
            {},
            {},
        ),
        ({}, {}, {}),
    ],
    ids=[
        "schema_default",
        "constant_value",
        "stored_template",
        "section_fields_flattened",
        "stored_wins_over_default",
        "stored_false_wins_over_true_default",
        "stored_none_falls_back_to_default",
        "stored_wins_over_constant",
        "field_without_any_value_skipped",
        "empty_form",
    ],
)
async def test_the_probe_reads_what_the_options_form_shows(
    core_client: HomeAssistantClient,
    probe_entry: ProbeEntry,
    fields: dict[Any, Any],
    stored: dict[str, Any],
    read: dict[str, Any],
) -> None:
    """#1457, #1575: an entry's options are what its options form pre-fills:
    the stored value over the schema default, a section's fields at the top
    level. A form that pre-fills nothing is still a successful read."""
    entry = probe_entry(_form_flow(vol.Schema(fields)), stored)

    assert await fetch_entry_options_with_status(core_client, entry.entry_id) == (
        read,
        True,
    )


async def test_an_options_menu_is_an_unread_probe_and_is_closed(
    hass: HomeAssistant, core_client: HomeAssistantClient, probe_entry: ProbeEntry
) -> None:
    """A menu holds no options, so the read failed (smart_search flags its
    search partial), and the opened flow is aborted."""
    entry = probe_entry(_MenuFlow, {"kept": 1})

    with patch.object(
        core_client, "abort_options_flow", wraps=core_client.abort_options_flow
    ) as abort:
        read = await fetch_entry_options_with_status(core_client, entry.entry_id)

    assert read == ({}, False)
    abort.assert_awaited_once()
    assert hass.config_entries.options.async_progress() == []


async def test_an_options_flow_core_cannot_start_is_an_unread_probe(
    core_client: HomeAssistantClient, probe_entry: ProbeEntry
) -> None:
    """No flow was opened, so there is none to abort."""
    entry = probe_entry(None, {"kept": 1})

    with patch.object(
        core_client, "abort_options_flow", wraps=core_client.abort_options_flow
    ) as abort:
        read = await fetch_entry_options_with_status(core_client, entry.entry_id)

    assert read == ({}, False)
    abort.assert_not_awaited()


async def test_an_unread_options_schema_is_a_warning_not_empty_options(
    core_client: HomeAssistantClient, probe_entry: ProbeEntry
) -> None:
    """#1575: a failed schema probe must not pass off as ``options: {}``."""
    entry = probe_entry(None, {"kept": 1})
    resp: dict[str, Any] = {"entry": {"options": {}}}

    await fetch_options_schema(core_client, entry.entry_id, resp)

    assert "options_schema" not in resp
    assert entry.entry_id in resp["warnings"][0]
