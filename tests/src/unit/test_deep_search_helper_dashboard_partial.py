"""A config search that could not read every helper or dashboard says so.

``deep_search`` runs on the server's own client against a real ``hass``. Core
supplies each condition: an ``input_*`` integration that is not loaded refuses
its list, a ``hass`` without ``config`` has no config-entries endpoint, a
dashboard saved as a string or deleted mid-search cannot be scanned. A helper
or dashboard backend that failed must come back as ``partial`` with the gap
named, never as a clean-looking empty result; one that answered must not.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import SOURCE_IGNORE, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp.client.rest_client import (
    NON_ADMIN_TOKEN_WARNING,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import SmartSearchTools

from ._command_faults import dropped_on, pooled_socket, received
from ._component_frames import older_component
from ._core_setup import install_custom_helper, set_up_domain
from ._deep_search_core import search_tools
from ._held_command import held_command
from ._mock_flow_integration import options_only_config_flow
from ._search_routing import DASHBOARDS
from ._template_helper_core import create_template_sensor

INPUT_HELPERS = (
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
    "input_button",
)
CUSTOM = "my_custom_helper"
NO_MATCH = "zzznomatch"
EMPTY_VIEWS: dict[str, Any] = {"views": []}
MARKER_VIEWS: dict[str, Any] = {"views": [{"title": "marker"}]}
LOVELACE_CONFIG = "lovelace/config"


class _GateOptions(OptionsFlow):
    """The custom helper's options flow: one field."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema({probatio.Optional("gate"): str}),
        )


@pytest.fixture
def custom_helper(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enable_custom_integrations: None,
) -> Iterator[None]:
    """``my_custom_helper``: a custom helper integration with an options flow,
    which Core's loader finds on disk and lists as a helper flow."""
    install_custom_helper(tmp_path, monkeypatch, CUSTOM)
    mock_integration(hass, MockModule(CUSTOM))
    mock_platform(hass, f"{CUSTOM}.config_flow")
    with mock_config_flow(CUSTOM, options_only_config_flow(_GateOptions)):
        yield


async def _load(hass: HomeAssistant, *domains: str) -> None:
    for domain in domains:
        await set_up_domain(hass, domain, {})


async def _search(
    client: HomeAssistantClient,
    search_types: list[str],
    query: str = NO_MATCH,
    **arguments: Any,
) -> dict[str, Any]:
    response: dict[str, Any] = await SmartSearchTools(client=client).deep_search(
        query=query, search_types=search_types, limit=10, **arguments
    )
    return response


def _assert_gap(response: dict[str, Any], pattern: str) -> None:
    assert response.get("partial") is True, response
    assert re.search(pattern, response["partial_reason"]), response["partial_reason"]


def _assert_complete(response: dict[str, Any]) -> None:
    assert "partial" not in response, response.get("partial_reason")


def _probe_spy(client: HomeAssistantClient) -> Any:
    """A spy on the options-flow probe; it calls through, so Core answers."""
    return patch.object(client, "start_options_flow", wraps=client.start_options_flow)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def test_a_helper_list_without_an_answer_is_counted_not_a_clean_zero(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The connection drops on a helper list: that backend was not scanned, so
    a caller cannot read the empty result as "no helper matches". An admin
    token is not blamed for it."""
    await _load(hass, "config", *INPUT_HELPERS)

    with dropped_on(hass, "input_boolean/list", await pooled_socket(core_client)):
        response = await _search(core_client, ["helper"])

    _assert_gap(response, r"\b[1-6] helper backend\(s\) not scanned")
    assert NON_ADMIN_TOKEN_WARNING not in response.get("warnings", [])


async def test_unreadable_config_entries_are_a_helper_scan_gap(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Every ``input_*`` list answers, but without ``config`` Core has no
    config-entries endpoint: the flow helpers are one backend not scanned."""
    await _load(hass, *INPUT_HELPERS)

    response = await _search(core_client, ["helper"])

    _assert_gap(response, r"\b1 helper backend\(s\) not scanned")


@pytest.mark.usefixtures("custom_helper")
async def test_custom_helper_body_is_not_opened_and_is_reported_unscanned(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A custom helper integration's options flow is its own code and may act
    when opened (#2987): the search does not open it, and says its entry was
    not scanned instead of reading the miss as a clean non-match."""
    await _load(hass, "config", *INPUT_HELPERS)
    MockConfigEntry(domain=CUSTOM, title="Gate Opener").add_to_hass(hass)

    with _probe_spy(core_client) as probe:
        response = await _search(core_client, ["helper"])

    probe.assert_not_awaited()
    _assert_gap(response, r"\b1 custom helper integration entry not scanned")


async def test_refused_admin_reads_name_the_token(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    """The config-entry and own-helper reads are admin-only: with a non-admin
    token the flow helpers go unscanned and the search says the token is why."""
    await create_template_sensor(hass, "on")
    # ``auth`` answers whose token it is.
    await _load(hass, "auth", *INPUT_HELPERS)

    response = await _search(read_only_client, ["helper"])

    assert response.get("partial") is True, response
    assert NON_ADMIN_TOKEN_WARNING in response.get("warnings", [])


async def test_options_probe_without_answer_is_a_helper_scan_gap(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A template helper whose title does not match needs its options read;
    when that read gets no answer the helper's body was never searched, which
    is one backend not scanned, not a "no match"."""
    await create_template_sensor(hass, "on")
    await _load(hass, *INPUT_HELPERS)

    with patch.object(
        core_client,
        "start_options_flow",
        AsyncMock(side_effect=HomeAssistantConnectionError("options flow down")),
    ):
        response = await _search(core_client, ["helper"])

    _assert_gap(response, r"\b1 helper backend\(s\) not scanned")


async def test_an_ignored_flow_helper_is_not_probed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An ignored entry was never set up and has no options flow by policy:
    the search reads the set-up helper's options form and leaves the ignored
    one alone, and a read form is a complete scan."""
    entry = await create_template_sensor(hass, "on")
    MockConfigEntry(
        domain="template", title="Ignored Template", source=SOURCE_IGNORE
    ).add_to_hass(hass)
    await _load(hass, *INPUT_HELPERS)

    with _probe_spy(core_client) as probe:
        response = await _search(core_client, ["helper"])

    probe.assert_awaited_once_with(entry.entry_id)
    _assert_complete(response)


async def test_helper_backends_that_all_answer_leave_the_result_complete(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Every helper list and the config entries answered with nothing to
    match: a true zero, not marked partial."""
    await _load(hass, "config", *INPUT_HELPERS)

    _assert_complete(await _search(core_client, ["helper"]))


# --------------------------------------------------------------------------
# Dashboards, legacy walk (no component)
# --------------------------------------------------------------------------


async def _dashboard(
    client: HomeAssistantClient,
    url_path: str,
    title: str,
    config: dict[str, Any] | str | None,
) -> str:
    """Create a storage dashboard (with ``config`` saved, if given); its id."""
    created = await client.send_websocket_message(
        {"type": "lovelace/dashboards/create", "url_path": url_path, "title": title}
    )
    assert created["success"], created
    if config is not None:
        saved = await client.send_websocket_message(
            {"type": "lovelace/config/save", "url_path": url_path, "config": config}
        )
        assert saved["success"], saved
    return str(created["result"]["id"])


async def test_without_lovelace_the_list_and_default_dashboard_are_counted(
    core_client: HomeAssistantClient,
) -> None:
    """Without Lovelace Core refuses the dashboard list and the default
    dashboard's config: both are counted as not scanned, the list's failure
    included rather than collapsed into an empty list."""
    response = await _search(core_client, ["dashboard"])

    _assert_gap(response, r"\b2 dashboard\(s\) not scanned")


async def test_dashboard_reads_without_answer_are_counted_per_dashboard(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The list answers, but no dashboard's config read does: the default and
    the stored dashboard are each one dashboard not scanned."""
    await _load(hass, "lovelace")
    await _dashboard(core_client, "lovelace-extra", "Extra", EMPTY_VIEWS)

    response = await search_tools(core_client, config_timeout=0).deep_search(
        NO_MATCH, ["dashboard"], limit=10
    )

    _assert_gap(response, r"\b2 dashboard\(s\) not scanned")


async def test_a_dashboard_config_that_is_not_a_mapping_is_a_scan_gap(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core stores a dashboard saved as a string and serves it back as one:
    that config cannot be searched, so it is counted, while the default
    dashboard read cleanly."""
    await _load(hass, "lovelace")
    await _dashboard(core_client, "lovelace-extra", "Extra", "not a dashboard")

    response = await _search(core_client, ["dashboard"])

    _assert_gap(response, r"\b1 dashboard\(s\) not scanned")


async def test_a_dashboard_deleted_during_the_search_is_a_scan_gap(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A dashboard deleted between the list and its config read gets Core's
    "Unknown config specified": unlike a dashboard with no stored config, it
    was never scanned and is counted."""
    await _load(hass, "lovelace")
    dashboard_id = await _dashboard(core_client, "gone-dash", "Gone", EMPTY_VIEWS)

    async with (
        asyncio.timeout(10),
        held_command(hass, LOVELACE_CONFIG) as held,
    ):
        search = asyncio.ensure_future(_search(core_client, ["dashboard"]))
        await held.arrived.wait()
        deleted = await core_client.send_websocket_message(
            {"type": "lovelace/dashboards/delete", "dashboard_id": dashboard_id}
        )
        assert deleted["success"], deleted
        held.release.set()
        response = await search

    _assert_gap(response, r"\b1 dashboard\(s\) not scanned")


async def test_unsaved_and_clean_dashboards_leave_the_result_complete(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A dashboard never taken control of has no stored config ("No config
    found."), which is nothing to scan rather than a failure (#2008); a
    scanned config without the query is a clean zero."""
    await _load(hass, "lovelace")
    await _dashboard(core_client, "auto-gen", "Auto", None)
    await _dashboard(core_client, "lovelace-extra", "Extra", EMPTY_VIEWS)

    _assert_complete(await _search(core_client, ["dashboard"]))


async def test_a_legacy_dashboard_match_names_the_dashboard_by_url_path(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A match from the per-dashboard walk carries ``url_path`` and ``title``,
    the keys every dashboard record uses (#2462)."""
    await _load(hass, "lovelace")
    await _dashboard(core_client, "my-dashboard", "My Dashboard", MARKER_VIEWS)

    response = await _search(core_client, ["dashboard"], query="marker")

    [record] = response["dashboards"]
    assert record["url_path"] == "my-dashboard"
    assert record["title"] == "My Dashboard"
    assert "dashboard_url" not in record
    assert "dashboard_title" not in record


# --------------------------------------------------------------------------
# Dashboards through the component's document search
# --------------------------------------------------------------------------


def _component_searched(seen: dict[str, list[Any]]) -> bool:
    return any(message.get("mode") == "search" for message in seen[DASHBOARDS])


async def test_component_verdicts_become_dashboard_records(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The component's whole-document verdicts serve the bucket with no
    per-dashboard config read, in the legacy record shape: the registry title,
    and the default dashboard labelled as the legacy walk labels it even when
    its own config carries a title."""
    client = component_core_client
    await _load(hass, "lovelace")
    await _dashboard(client, "energy-board", "Energy Registry", MARKER_VIEWS)
    saved = await client.send_websocket_message(
        {
            "type": "lovelace/config/save",
            "config": {"title": "My Custom Home", **MARKER_VIEWS},
        }
    )
    assert saved["success"], saved

    with received(hass, LOVELACE_CONFIG, DASHBOARDS) as seen:
        response = await _search(client, ["dashboard"], query="marker")

    assert _component_searched(seen)
    assert seen[LOVELACE_CONFIG] == []
    records = {record["url_path"]: record for record in response["dashboards"]}
    assert set(records) == {"energy-board", "default"}
    assert records["energy-board"]["title"] == "Energy Registry"
    assert records["default"]["title"] == "Default Dashboard"
    assert all(record["match_in_config"] is True for record in records.values())
    _assert_complete(response)


async def test_a_yaml_dashboard_sends_the_search_to_the_legacy_walk(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The component never reads YAML dashboard bodies, the legacy walk does:
    with a YAML dashboard the legacy walk serves the bucket, so coverage does
    not depend on the route and the result is complete, not partial."""
    await hass.async_add_executor_job(
        Path(hass.config.path("yaml-board.yaml")).write_text, "views: []\n"
    )
    yaml_board = {"mode": "yaml", "filename": "yaml-board.yaml", "title": "Yaml"}
    await set_up_domain(hass, "lovelace", {"dashboards": {"yaml-board": yaml_board}})

    with received(hass, LOVELACE_CONFIG, DASHBOARDS) as seen:
        response = await _search(component_core_client, ["dashboard"])

    assert _component_searched(seen)
    assert "yaml-board" in {m.get("url_path") for m in seen[LOVELACE_CONFIG]}
    assert response["dashboards"] == []
    _assert_complete(response)


async def test_a_dashboard_the_component_cannot_load_is_a_scan_gap(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A storage dashboard whose config the component cannot read as a
    mapping is counted as not scanned on the component route too."""
    await _load(hass, "lovelace")
    await _dashboard(
        component_core_client, "lovelace-extra", "Extra", "not a dashboard"
    )

    with received(hass, LOVELACE_CONFIG, DASHBOARDS) as seen:
        response = await _search(component_core_client, ["dashboard"])

    assert _component_searched(seen)
    assert seen[LOVELACE_CONFIG] == []
    _assert_gap(response, r"\b1 dashboard\(s\) not scanned")


async def test_a_component_without_document_search_stays_on_the_legacy_walk(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older component only has the card-scoped search, which would
    narrow coverage: its search frame is not sent and the legacy walk runs."""
    older_component(monkeypatch, "dashboards_doc_search")
    await _load(hass, "lovelace")

    with received(hass, LOVELACE_CONFIG, DASHBOARDS) as seen:
        await _search(component_core_client, ["dashboard"])

    assert not _component_searched(seen)
    assert seen[LOVELACE_CONFIG]


@pytest.mark.parametrize(
    "arguments",
    [{"exact_match": False}, {"include_config": True}],
    ids=["fuzzy", "include_config"],
)
async def test_fuzzy_or_body_search_stays_on_the_legacy_walk(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    arguments: dict[str, Any],
) -> None:
    """Fuzzy scoring and returned config bodies are what the component's
    substring search cannot give: its frame is not sent and the legacy walk
    runs."""
    await _load(hass, "lovelace")

    with received(hass, LOVELACE_CONFIG, DASHBOARDS) as seen:
        await _search(component_core_client, ["dashboard"], **arguments)

    assert not _component_searched(seen)
    assert seen[LOVELACE_CONFIG]


async def test_a_failed_component_search_falls_back_to_the_legacy_walk(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the component's dashboard search fails, Core answers with an
    error and the legacy walk serves the bucket: the match is still found."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise HomeAssistantError("boom")

    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.websocket_api.dashboards"
        "._dashboard_document_matches",
        _boom,
    )
    await _load(hass, "lovelace")
    await _dashboard(component_core_client, "my-dashboard", "Mine", MARKER_VIEWS)

    with received(hass, LOVELACE_CONFIG, DASHBOARDS) as seen:
        response = await _search(component_core_client, ["dashboard"], query="marker")

    assert _component_searched(seen)
    assert seen[LOVELACE_CONFIG]
    assert [d["url_path"] for d in response["dashboards"]] == ["my-dashboard"]
    _assert_complete(response)
