"""The ``partial_reason`` a config search gives when it could not read every config.

``deep_search`` runs on the server's own client against a real ``hass``. Core
supplies each condition: a YAML-defined automation or script is a 404 on the
per-id config endpoint, a ``!secret`` in the file that endpoint loads is an
HTTP 500 for every id, a value Core cannot serialize is an HTTP 500 for that
id alone, and a slow server is a fetch that gives no answer in time. Each
fragment must say plainly that the configs were not scanned, that their match
status is unknown and that the result is not exhaustive: softer wording was
read as a complete result by agents (PR #1529).
"""

from __future__ import annotations

import asyncio
import itertools
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import INDIVIDUAL_FETCH_BATCH_SIZE, SmartSearchTools

QUERY = "kitchen"
LOG_HINT = "in the Home Assistant log"
# The per-id endpoint loads the whole file and rejects a secret before it looks
# the id up, so every fetch of that type answers HTTP 500 (#1784 follow-up).
UNREADABLE_AUTOMATIONS = "- id: a0\n  alias: A0\n  description: !secret nope\n"
UNREADABLE_SCRIPTS = "s0:\n  alias: !secret nope\n  sequence: []\n"
# The per-id endpoint loads this file and finds a1, then cannot serialize its
# bytes value, so a1 alone answers HTTP 500; any other id is a 404.
UNSERIALIZABLE_A1 = "- id: a1\n  alias: A1\n  description: !!binary aGk=\n"
# The registry lookup that resolves a script's storage key before its fetch.
REGISTRY_GET = "config/entity_registry/get"
# Each fetch waits for Core's real answer, however long the machine takes to
# give it: a fetch that runs out of time is reported as a timeout, not as the
# answer a test sets up. The tests that report a timeout cause it themselves.
NO_FETCH_TIMEOUT = 600.0


@pytest.fixture
async def config_views(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def search(config_views: None, core_client: HomeAssistantClient) -> SmartSearchTools:
    return SmartSearchTools(client=core_client, config_timeout=NO_FETCH_TIMEOUT)


async def _yaml_automations(hass: HomeAssistant, count: int) -> None:
    """Automations defined in configuration.yaml: each has an id, none is in automations.yaml."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": f"a{i}",
                    "alias": f"A{i}",
                    "triggers": [{"trigger": "event", "event_type": "test"}],
                    "actions": [],
                }
                for i in range(count)
            ]
        },
    )


async def _yaml_scripts(hass: HomeAssistant, count: int) -> None:
    """Scripts defined in configuration.yaml, none of them in scripts.yaml."""
    assert await async_setup_component(
        hass, "script", {"script": {f"s{i}": {"sequence": []} for i in range(count)}}
    )


async def _yaml_entities(hass: HomeAssistant, noun: str, count: int) -> None:
    if noun == "automation":
        await _yaml_automations(hass, count)
    else:
        await _yaml_scripts(hass, count)


async def _write_config_file(hass: HomeAssistant, name: str, text: str) -> None:
    await hass.async_add_executor_job(Path(hass.config.path(name)).write_text, text)


async def _reason(search: SmartSearchTools, *search_types: str, **kwargs: Any) -> str:
    response = await search.deep_search(QUERY, list(search_types), **kwargs)
    assert response["partial"] is True
    reason: str = response["partial_reason"]
    return reason


def _assert_states_incompleteness(fragment: str) -> None:
    assert "match status is unknown" in fragment
    assert "not exhaustive" in fragment


def _fragment(reason: str, prefix: str) -> str:
    """The one ``partial_reason`` fragment that starts with ``prefix``."""
    matching = [part for part in reason.split(" ; ") if part.startswith(prefix)]
    assert len(matching) == 1, reason
    return matching[0]


@pytest.mark.parametrize(
    ("noun", "budget_setting"),
    [
        ("automation", "HAMCP_AUTOMATION_CONFIG_TIME_BUDGET"),
        ("script", "HAMCP_SCRIPT_CONFIG_TIME_BUDGET"),
    ],
)
@pytest.mark.usefixtures("config_views")
async def test_exhausted_budget_names_the_unscanned_configs_and_their_setting(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    noun: str,
    budget_setting: str,
) -> None:
    """Configs left when the time budget runs out are counted and point at the type's own budget setting."""
    total = INDIVIDUAL_FETCH_BATCH_SIZE + 1
    await _yaml_entities(hass, noun, total)
    # Each clock reading is one second after the last: 0 when the fetch
    # starts, 1 before the first batch, 2 before the second. A 1.5 s budget
    # runs out between the two, so exactly the second batch is left.
    ticks = itertools.count()
    search = SmartSearchTools(
        client=core_client,
        clock=lambda: float(next(ticks)),
        config_timeout=NO_FETCH_TIMEOUT,
    )

    reason = await _reason(search, noun, config_time_budget=1.5)

    counted = re.search(
        rf"\b(\d+) {noun}\(s\) not scanned \(time budget exhausted\)", reason
    )
    assert counted, reason
    assert int(counted.group(1)) == total - INDIVIDUAL_FETCH_BATCH_SIZE
    _assert_states_incompleteness(reason)
    assert budget_setting in reason


@pytest.mark.usefixtures("config_views")
async def test_budget_exhaustion_and_failed_fetches_are_counted_apart(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Configs the budget left unread and configs whose fetch failed each get their own count."""
    total = INDIVIDUAL_FETCH_BATCH_SIZE + 1
    await _write_config_file(hass, "automations.yaml", UNREADABLE_AUTOMATIONS)
    await _yaml_automations(hass, total)
    # The same clock as the budget test above: the first batch is fetched
    # (and fails), the budget runs out before the second.
    ticks = itertools.count()
    search = SmartSearchTools(
        client=core_client,
        clock=lambda: float(next(ticks)),
        config_timeout=NO_FETCH_TIMEOUT,
    )

    reason = await _reason(search, "automation", config_time_budget=1.5)

    failed = _fragment(
        reason,
        f"{INDIVIDUAL_FETCH_BATCH_SIZE} automation(s) not scanned "
        "(per-id fetch raised a non-404 error",
    )
    skipped = _fragment(
        reason,
        f"{total - INDIVIDUAL_FETCH_BATCH_SIZE} automation(s) not scanned "
        "(time budget exhausted)",
    )
    assert LOG_HINT in failed
    assert "HAMCP_AUTOMATION_CONFIG_TIME_BUDGET" in skipped


@pytest.mark.parametrize(
    ("noun", "endpoint"),
    [
        ("automation", "/config/automation/config"),
        ("script", "/config/script/config"),
    ],
)
async def test_yaml_defined_config_is_reported_as_a_structural_gap(
    hass: HomeAssistant, search: SmartSearchTools, noun: str, endpoint: str
) -> None:
    """A config the per-id endpoint answers 404 for is named YAML-defined, not a fetch failure (PR #1529)."""
    await _yaml_entities(hass, noun, 2)

    reason = await _reason(search, noun)

    assert f"2 {noun}(s) not scanned (per-id config endpoint returned 404" in reason
    assert f"YAML-defined {noun}s that the {endpoint} REST endpoint" in reason
    _assert_states_incompleteness(reason)
    assert "non-404" not in reason


async def test_yaml_defined_and_failed_configs_of_one_type_are_reported_apart(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """A 404 and a non-404 error on one type stay two fragments: the structural gap is not retryable, the failure may be (PR #1529)."""
    await _write_config_file(hass, "automations.yaml", UNSERIALIZABLE_A1)
    await _yaml_automations(hass, 2)

    reason = await _reason(search, "automation")

    failed = _fragment(
        reason, "1 automation(s) not scanned (per-id fetch raised a non-404 error"
    )
    yaml_defined = _fragment(
        reason, "1 automation(s) not scanned (per-id config endpoint returned 404"
    )
    assert "YAML-defined" not in failed
    assert "YAML-defined automations" in yaml_defined
    _assert_states_incompleteness(failed)
    _assert_states_incompleteness(yaml_defined)


@pytest.mark.parametrize(
    ("noun", "file_name", "text"),
    [
        ("automation", "automations.yaml", UNREADABLE_AUTOMATIONS),
        ("script", "scripts.yaml", UNREADABLE_SCRIPTS),
    ],
)
async def test_http_500_fetch_names_the_error_and_the_log(
    hass: HomeAssistant,
    search: SmartSearchTools,
    noun: str,
    file_name: str,
    text: str,
) -> None:
    """A per-id fetch that answers HTTP 500 names that error and where its cause is logged (#1784)."""
    await _write_config_file(hass, file_name, text)
    await _yaml_entities(hass, noun, 2)

    reason = await _reason(search, noun)

    assert (
        f"2 {noun}(s) not scanned (per-id fetch raised a non-404 error; "
        "e.g. HTTP 500: 500 Internal Server Error)" in reason
    )
    _assert_states_incompleteness(reason)
    assert "`!secret` reference in the config file HA loads" in reason
    assert LOG_HINT in reason
    assert "YAML-defined" not in reason


async def test_timed_out_fetch_points_at_the_concurrency_settings(
    hass: HomeAssistant, core_client: HomeAssistantClient, search: SmartSearchTools
) -> None:
    """A fetch with no answer in time is reported as a timeout with its tuning settings, not as a failure (#1784)."""
    await _yaml_scripts(hass, 2)

    with patch.object(
        core_client, "get_script_config", AsyncMock(side_effect=TimeoutError())
    ):
        reason = await _reason(search, "script")

    assert "2 script(s) not scanned (per-id fetch timed out" in reason
    assert "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE" in reason
    assert "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT" in reason
    _assert_states_incompleteness(reason)
    assert "non-404" not in reason


async def test_timed_out_and_failed_configs_of_one_type_are_reported_apart(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    search: SmartSearchTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout and a failure on one type stay two fragments, each with its own remedy (#1784).

    Core holds the registry lookup for script s0 until the client's wait for
    that WebSocket command has run out; s1 is answered, and its config fetch
    is Core's HTTP 500 for the ``!secret`` in scripts.yaml.
    """
    await _write_config_file(hass, "scripts.yaml", UNREADABLE_SCRIPTS)
    await _yaml_scripts(hass, 2)
    held = asyncio.Event()
    release = asyncio.Event()
    answered = asyncio.Event()
    core_handler, schema = hass.data[websocket_api.DOMAIN][REGISTRY_GET]

    async def hold_s0_lookup(
        core: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        if msg["entity_id"] == "script.s0":
            held.set()
            await release.wait()
            core_handler(core, connection, msg)
            answered.set()
        else:
            core_handler(core, connection, msg)

    websocket_api.async_register_command(
        hass, REGISTRY_GET, websocket_api.async_response(hold_s0_lookup), schema
    )
    # One real command opens the client's socket under the normal wait; the
    # wait is then cut so only the held lookup runs out of it.
    await core_client.send_websocket_message({"type": "config/entity_registry/list"})
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 1.0)
    try:
        reason = await _reason(search, "script")
    finally:
        # Answer the held lookup while the client's socket is still open.
        release.set()
        if held.is_set():
            async with asyncio.timeout(5):
                await answered.wait()
            # Core writes replies in order: the pong means the held reply is out.
            await core_client.send_websocket_message({"type": "ping"})

    assert held.is_set()
    timed_out = _fragment(reason, "1 script(s) not scanned (per-id fetch timed out")
    failed = _fragment(
        reason, "1 script(s) not scanned (per-id fetch raised a non-404 error"
    )
    assert "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT" in timed_out
    assert LOG_HINT not in timed_out
    assert LOG_HINT in failed
    assert "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT" not in failed


async def test_failure_sample_and_log_hint_stay_with_their_type(
    hass: HomeAssistant, core_client: HomeAssistantClient, search: SmartSearchTools
) -> None:
    """Each type names its own error, and only the HTTP 500 carries the log hint, even when another error mentions 500."""
    await _write_config_file(hass, "automations.yaml", UNREADABLE_AUTOMATIONS)
    await _yaml_automations(hass, 1)
    await _yaml_scripts(hass, 1)

    with patch.object(
        core_client,
        "get_script_config",
        AsyncMock(
            side_effect=HomeAssistantConnectionError(
                "connection dropped after HTTP 500"
            )
        ),
    ):
        reason = await _reason(search, "automation", "script")

    automation_part, script_part = reason.split(" ; ")
    assert automation_part.startswith(
        "1 automation(s) not scanned (per-id fetch raised a non-404 error; "
        "e.g. HTTP 500: 500 Internal Server Error)"
    )
    assert LOG_HINT in automation_part
    assert script_part.startswith(
        "1 script(s) not scanned (per-id fetch raised a non-404 error; "
        "e.g. HomeAssistantConnectionError: connection dropped after HTTP 500)"
    )
    assert LOG_HINT not in script_part


async def test_http_500_on_both_types_hints_in_each_fragment(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """When automations and scripts both answer HTTP 500, each fragment carries its own log hint."""
    await _write_config_file(hass, "automations.yaml", UNREADABLE_AUTOMATIONS)
    await _write_config_file(hass, "scripts.yaml", UNREADABLE_SCRIPTS)
    await _yaml_automations(hass, 1)
    await _yaml_scripts(hass, 1)

    reason = await _reason(search, "automation", "script")

    assert [LOG_HINT in part for part in reason.split(" ; ")] == [True, True]


async def test_unreachable_helper_backend_is_not_a_clean_zero(
    core_client: HomeAssistantClient, search: SmartSearchTools
) -> None:
    """Helper lists that give no answer are reported as not scanned, not as no matches."""
    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection dropped")),
    ):
        reason = await _reason(search, "helper")

    assert re.search(r"\b\d+ helper backend\(s\) not scanned", reason), reason
    _assert_states_incompleteness(reason)


async def test_unreadable_dashboard_is_not_a_clean_zero(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """A dashboard whose config Core cannot load is reported as not scanned, not as no matches."""
    await _write_config_file(hass, "broken-dashboard.yaml", "views: [\n")
    assert await async_setup_component(
        hass,
        "lovelace",
        {
            "lovelace": {
                "dashboards": {
                    "broken-dashboard": {
                        "mode": "yaml",
                        "filename": "broken-dashboard.yaml",
                        "title": "Broken",
                    }
                }
            }
        },
    )

    reason = await _reason(search, "dashboard")

    assert "1 dashboard(s) not scanned" in reason
    _assert_states_incompleteness(reason)


async def test_scene_reason_is_kept_when_automations_add_theirs(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """The automation fragment is appended after the scene fragment, not written over it."""
    assert await async_setup_component(
        hass,
        "scene",
        {"scene": [{"id": "sc0", "name": "Sc0", "entities": {"light.x": "on"}}]},
    )
    await _yaml_automations(hass, 1)

    reason = await _reason(search, "scene", "automation")

    assert reason.startswith("1 scene(s) not scanned")
    assert "1 automation(s) not scanned (per-id config endpoint returned 404" in reason


async def test_configs_the_endpoint_serves_leave_the_result_complete(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """A search that read every config is not marked partial."""
    await _write_config_file(
        hass,
        "automations.yaml",
        "- id: a0\n  alias: A0\n  triggers: []\n  actions: []\n",
    )
    await _write_config_file(hass, "scripts.yaml", "s0:\n  sequence: []\n")
    await _yaml_automations(hass, 1)
    await _yaml_scripts(hass, 1)

    response = await search.deep_search(QUERY, ["automation", "script"])

    assert "partial" not in response
    assert "partial_reason" not in response
