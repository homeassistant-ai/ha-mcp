"""One flow step per ``ha_set_integration`` call (integration_flow_steps).

Every flow below is a test integration's own, run by Core's flow managers;
the server's client (``core_client``) drives them over Core's flow endpoints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._backup_core import backup_settings as backup_settings
from ._integration_flow_steps_core import (
    ERROR_TEXTS,
    EXTERNAL,
    INSTANT,
    MAC,
    MENU,
    MENU_OPTIONS,
    REFUSED_ADDRESS,
    REJOIN,
    REREAD,
    SINGLE,
    STEPS,
    config_flow_ids,
    flow_calls,
    snapshot_count,
)
from ._integration_flow_steps_core import annex as annex
from ._integration_flow_steps_core import flow_tools as flow_tools
from ._integration_flow_steps_core import hub as hub
from ._mock_flow_integration import open_flow_ids
from ._tool_errors import rejected_call_body

# --- a new flow -------------------------------------------------------------


async def test_a_new_flow_answers_its_form_and_reports_the_entry_it_created(
    hass: HomeAssistant, flow_tools: IntegrationTools
) -> None:
    result = await flow_tools.ha_set_integration(
        domain=SINGLE, config={"host": "10.0.0.2"}
    )

    (entry,) = hass.config_entries.async_entries(SINGLE)
    assert result == {
        "success": True,
        "entry_id": entry.entry_id,
        "title": "10.0.0.2",
        "domain": SINGLE,
        "message": f"{SINGLE} integration created successfully",
    }
    assert config_flow_ids(hass) == []


async def test_a_config_list_answers_the_flows_forms_in_order(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    """Each object goes to the form the flow is then at, and an integration
    whose flow ends early reports the answers it never asked for."""
    answers = [
        {"connection_type": "routing"},
        {"individual_address": "0.0.240"},
        {"x": 1},
    ]

    with flow_calls(core_client) as calls:
        result = await flow_tools.ha_set_integration(domain=STEPS, config=answers)

    submitted = calls["submit_config_flow_step"].await_args_list
    assert [call.args[1] for call in submitted] == answers[:2]
    (entry,) = hass.config_entries.async_entries(STEPS)
    assert result["entry_id"] == entry.entry_id
    assert "before the last 1" in result["warnings"][0]


async def test_a_new_flow_answers_its_menu_then_the_form_in_one_call(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    with flow_calls(core_client) as calls:
        result = await flow_tools.ha_set_integration(
            domain=MENU,
            menu_choice="routing",
            config={"individual_address": "0.0.240"},
        )

    submitted = calls["submit_config_flow_step"].await_args_list
    assert [call.args[1] for call in submitted] == [
        {"next_step_id": "routing"},
        {"individual_address": "0.0.240"},
    ]
    (entry,) = hass.config_entries.async_entries(MENU)
    assert result["entry_id"] == entry.entry_id


@pytest.mark.parametrize(
    "answer", [{"menu_choice": "routing"}, {"config": {"host": "h"}}]
)
async def test_a_flow_finished_at_its_start_submits_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
    answer: dict[str, Any],
) -> None:
    with flow_calls(core_client) as calls:
        result = await flow_tools.ha_set_integration(domain=INSTANT, **answer)

    (entry,) = hass.config_entries.async_entries(INSTANT)
    assert result["entry_id"] == entry.entry_id
    calls["submit_config_flow_step"].assert_not_called()


async def test_a_new_flow_aborted_on_its_answer_gives_cores_reason(
    hass: HomeAssistant, flow_tools: IntegrationTools
) -> None:
    MockConfigEntry(domain=SINGLE, unique_id="10.0.0.2").add_to_hass(hass)

    body = await rejected_call_body(
        flow_tools.ha_set_integration(domain=SINGLE, config={"host": "10.0.0.2"})
    )

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "already_configured" in body["error"]["message"]
    assert len(hass.config_entries.async_entries(SINGLE)) == 1


# --- the answer to a step ---------------------------------------------------


async def test_a_rejected_form_is_an_error_and_keeps_the_flow_open(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    opened = await flow_tools.ha_set_integration(
        domain=STEPS, config={"connection_type": "routing"}
    )

    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            flow_tools.ha_set_integration(
                domain=STEPS,
                flow_id=opened["flow_id"],
                config={"individual_address": REFUSED_ADDRESS},
            )
        )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["flow_id"] == opened["flow_id"]
    assert body["errors"] == {"base": "no_router"}
    assert body["error_text"] == {"base": ERROR_TEXTS["no_router"]}
    assert "The flow is still open" in " ".join(body["error"]["suggestions"])
    calls["abort_config_flow"].assert_not_called()
    assert config_flow_ids(hass) == [opened["flow_id"]]


async def test_a_form_shown_first_with_advisory_errors_is_a_step(
    hass: HomeAssistant, flow_tools: IntegrationTools
) -> None:
    """KNX's routing form opens with no_router_discovered after
    connection_type: a new step with errors, not the submitted one shown again."""
    result = await flow_tools.ha_set_integration(
        domain=STEPS, config={"connection_type": "routing"}
    )

    assert result["success"] is True
    assert result["step_id"] == "routing"
    assert result["errors"] == {"base": "no_router_discovered"}
    assert config_flow_ids(hass) == [result["flow_id"]]


async def test_a_menu_answer_lists_its_options(
    flow_tools: IntegrationTools,
) -> None:
    result = await flow_tools.ha_set_integration(
        domain=STEPS, config={"connection_type": "menu"}
    )

    assert result["flow_type"] == "menu"
    assert result["menu_options"] == MENU_OPTIONS
    assert "menu_choice" in result["message"]


async def test_an_undeclared_key_is_left_to_home_assistant_to_refuse(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    """Core's schema decides what a form takes: the key is sent, and Core's
    refusal comes back with the form's schema and the flow still open."""
    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            flow_tools.ha_set_integration(
                domain=SINGLE, config={"host": "h", "port": 1}
            )
        )

    submitted = calls["submit_config_flow_step"]
    submitted.assert_awaited_once()
    assert submitted.await_args is not None
    assert submitted.await_args.args[1] == {"host": "h", "port": 1}
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "'port'" in body["error"]["message"]
    assert [field["name"] for field in body["data_schema"]] == ["host", "name"]
    assert any("still open" in hint for hint in body["error"]["suggestions"])
    calls["abort_config_flow"].assert_not_called()
    assert config_flow_ids(hass) == [body["flow_id"]]


async def test_an_options_form_keeps_the_values_the_caller_left_out(
    flow_tools: IntegrationTools, hub: MockConfigEntry
) -> None:
    result = await flow_tools.ha_set_integration(
        entry_id=hub.entry_id, config={"string": "BAT"}
    )

    assert result["updated"] is True
    assert result["entry_id"] == hub.entry_id
    assert dict(hub.options) == {"string": "BAT", "int": 10}


async def test_a_setup_form_does_not_get_the_values_the_caller_left_out(
    hass: HomeAssistant, flow_tools: IntegrationTools
) -> None:
    """A setup form's suggested value is a hint, not something to submit."""
    await flow_tools.ha_set_integration(domain=SINGLE, config={"host": "h"})

    (entry,) = hass.config_entries.async_entries(SINGLE)
    assert dict(entry.data) == {"host": "h"}


async def test_a_redaction_placeholder_in_config_is_refused_before_a_flow_opens(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            flow_tools.ha_set_integration(domain=SINGLE, config={"host": REDACTED_SET})
        )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "redaction placeholder" in body["error"]["message"]
    calls["start_config_flow"].assert_not_called()
    assert config_flow_ids(hass) == []


# --- a flow_id continues the open flow ---------------------------------------


@pytest.mark.parametrize("kind", ["setup", "options"])
async def test_flow_id_answers_the_step_its_flow_is_at(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
    hub: MockConfigEntry,
    kind: str,
) -> None:
    target = {"domain": SINGLE} if kind == "setup" else {"entry_id": hub.entry_id}
    answer = {"host": "h"} if kind == "setup" else {"string": "BAT"}
    opened = await flow_tools.ha_set_integration(**target)

    with flow_calls(core_client) as calls:
        result = await flow_tools.ha_set_integration(
            flow_id=opened["flow_id"], config=answer, **target
        )

    submit = calls[f"submit_{'config' if kind == 'setup' else 'options'}_flow_step"]
    submit.assert_awaited_once()
    assert submit.await_args is not None
    assert submit.await_args.args[0] == opened["flow_id"]
    assert result["success"] is True
    assert open_flow_ids(hass) == []


async def test_flow_id_alone_reads_the_step_again_without_submitting(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    """flow_id alone is a read, not the ``config = {}`` default of a new flow."""
    opened = await flow_tools.ha_set_integration(domain=SINGLE)

    with flow_calls(core_client) as calls:
        result = await flow_tools.ha_set_integration(
            domain=SINGLE, flow_id=opened["flow_id"]
        )

    assert result["flow_id"] == opened["flow_id"]
    assert result["step_id"] == "user"
    calls["start_config_flow"].assert_not_called()
    calls["submit_config_flow_step"].assert_not_called()
    assert config_flow_ids(hass) == [opened["flow_id"]]


async def _open_flow(
    hass: HomeAssistant,
    tools: IntegrationTools,
    opener: str,
    hub: MockConfigEntry,
) -> str:
    """A flow opened by the HA UI, or by the tool for another target."""
    if opener == "the-ui":
        started = await hass.config_entries.flow.async_init(
            STEPS, context={"source": SOURCE_USER}
        )
        return str(started["flow_id"])
    call: dict[str, Any] = {
        "another-integration": {"domain": MENU},
        "another-entry": {"entry_id": hub.entry_id},
        "a-reconfigure": {
            "entry_id": hub.entry_id,
            "reconfigure": True,
            "expected_mac": MAC,
        },
    }[opener]
    opened = await tools.ha_set_integration(**call)
    return str(opened["flow_id"])


@pytest.mark.parametrize(
    ("opener", "target"),
    [
        ("the-ui", "setup"),
        ("another-integration", "setup"),
        ("a-reconfigure", "setup"),
        ("another-entry", "options"),
    ],
)
@pytest.mark.parametrize("call", [{"config": {"host": "h"}}, {"abort": True}])
async def test_a_flow_not_opened_for_the_calls_target_is_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
    hub: MockConfigEntry,
    annex: MockConfigEntry,
    opener: str,
    target: str,
    call: dict[str, Any],
) -> None:
    """A flow_id continues or ends only with the kind and target this server
    opened it with: not another integration's or entry's flow, not a
    reconfigure flow as a setup flow, and not one it did not open (the UI's,
    or one from before an ha-mcp restart)."""
    flow_id = await _open_flow(hass, flow_tools, opener, hub)
    named = {"domain": STEPS} if target == "setup" else {"entry_id": annex.entry_id}

    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            flow_tools.ha_set_integration(flow_id=flow_id, **named, **call)
        )

    assert "of this call's target" in body["error"]["message"]
    for spy in calls.values():
        spy.assert_not_called()
    assert open_flow_ids(hass) == [flow_id]


@pytest.mark.parametrize(
    ("started_for", "continued"),
    [
        ("hub-reconfigure", True),
        ("annex-reconfigure", False),
        ("user-setup", True),
        ("hub-reauth", False),
    ],
)
async def test_a_flow_the_server_forgot_is_bound_by_cores_record(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
    hub: MockConfigEntry,
    annex: MockConfigEntry,
    started_for: str,
    continued: bool,
) -> None:
    """After an ha-mcp restart the flow's target is the one Core recorded: a
    reconfigure flow continues only for its own entry, and a reauth flow Core
    started for the domain is not a setup flow to answer."""
    if started_for == "hub-reconfigure":
        started = await hub.start_reconfigure_flow(hass)
    elif started_for == "annex-reconfigure":
        started = await annex.start_reconfigure_flow(hass)
    elif started_for == "hub-reauth":
        started = await hub.start_reauth_flow(hass)
    else:
        started = await hass.config_entries.flow.async_init(
            STEPS, context={"source": SOURCE_USER}
        )
    named: dict[str, Any] = (
        {"domain": STEPS}
        if started_for.endswith("setup") or started_for == "hub-reauth"
        else {"entry_id": hub.entry_id, "reconfigure": True, "expected_mac": MAC}
    )
    call = flow_tools.ha_set_integration(flow_id=started["flow_id"], **named)

    if continued:
        assert (await call)["flow_id"] == started["flow_id"]
    else:
        body = await rejected_call_body(call)
        assert "of this call's target" in body["error"]["message"]


async def test_a_flow_home_assistant_no_longer_has_is_not_found(
    hass: HomeAssistant, flow_tools: IntegrationTools
) -> None:
    opened = await flow_tools.ha_set_integration(domain=SINGLE)
    hass.config_entries.flow.async_abort(opened["flow_id"])

    body = await rejected_call_body(
        flow_tools.ha_set_integration(
            domain=SINGLE, flow_id=opened["flow_id"], config={"host": "h"}
        )
    )

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["flow_kind"] == "setup"


@pytest.mark.parametrize(
    ("domain", "menu_choice", "config", "says"),
    [
        (MENU, "secure", None, "not offered"),
        (SINGLE, "routing", None, "not a menu"),
        (MENU, None, {"host": "h"}, "pass menu_choice"),
        (MENU, "routing", {"host": "h"}, "not both"),
    ],
)
async def test_a_wrong_kind_of_answer_is_refused_and_the_flow_stays_open(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
    domain: str,
    menu_choice: str | None,
    config: dict[str, Any] | None,
    says: str,
) -> None:
    opened = await flow_tools.ha_set_integration(domain=domain)

    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            flow_tools.ha_set_integration(
                domain=domain,
                flow_id=opened["flow_id"],
                menu_choice=menu_choice,
                config=config,
            )
        )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert says in body["error"]["message"]
    assert "abort=True" in body["error"]["suggestion"]
    calls["submit_config_flow_step"].assert_not_called()
    calls["abort_config_flow"].assert_not_called()
    assert config_flow_ids(hass) == [opened["flow_id"]]


async def test_a_server_error_on_re_reading_a_flow_is_not_reported_as_applied(
    core_client: HomeAssistantClient, flow_tools: IntegrationTools
) -> None:
    """Core runs the step again to answer a read: its 500 is a failed read,
    not an answer lost after a submit that may have saved."""
    opened = await flow_tools.ha_set_integration(domain=REREAD)

    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            flow_tools.ha_set_integration(domain=REREAD, flow_id=opened["flow_id"])
        )

    assert "500" in body["error"]["message"]
    assert body.get("status") != "applied_but_unverified"
    calls["submit_config_flow_step"].assert_not_called()


# --- abort --------------------------------------------------------------------


async def test_a_failed_abort_keeps_the_flow_bound_and_a_gone_one_is_not_open(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    opened = await flow_tools.ha_set_integration(domain=SINGLE)
    abort = {"domain": SINGLE, "flow_id": opened["flow_id"], "abort": True}

    with patch.object(
        core_client,
        "abort_config_flow",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection dropped")),
    ):
        await rejected_call_body(flow_tools.ha_set_integration(**abort))

    # Still bound to this target: the abort can be sent again.
    assert (await flow_tools.ha_set_integration(**abort))["aborted"] is True
    assert config_flow_ids(hass) == []

    gone = await flow_tools.ha_set_integration(domain=SINGLE)
    hass.config_entries.flow.async_abort(gone["flow_id"])
    body = await rejected_call_body(
        flow_tools.ha_set_integration(
            domain=SINGLE, flow_id=gone["flow_id"], abort=True
        )
    )
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_abort_without_flow_id_is_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            flow_tools.ha_set_integration(domain=SINGLE, abort=True)
        )

    assert "flow_id" in body["error"]["message"]
    calls["start_config_flow"].assert_not_called()
    calls["abort_config_flow"].assert_not_called()
    assert config_flow_ids(hass) == []


async def test_abort_ends_the_setup_flow_the_tool_opened(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    opened = await flow_tools.ha_set_integration(domain=SINGLE)

    with flow_calls(core_client) as calls:
        result = await flow_tools.ha_set_integration(
            domain=SINGLE, flow_id=opened["flow_id"], abort=True
        )

    calls["abort_config_flow"].assert_awaited_once_with(opened["flow_id"])
    calls["submit_config_flow_step"].assert_not_called()
    assert result["aborted"] is True
    assert config_flow_ids(hass) == []


# --- what Core answers --------------------------------------------------------


async def test_an_external_step_is_refused_and_its_flow_aborted(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    with flow_calls(core_client) as calls:
        body = await rejected_call_body(flow_tools.ha_set_integration(domain=EXTERNAL))

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "browser" in body["error"]["message"]
    calls["abort_config_flow"].assert_awaited_once()
    assert config_flow_ids(hass) == []


async def test_a_setup_flow_that_updates_an_existing_entry_is_reported_as_applied(
    hass: HomeAssistant, flow_tools: IntegrationTools
) -> None:
    """A setup flow that ends with reauth_successful changed an entry no
    check of this call covered: applied, but not verified."""
    existing = MockConfigEntry(domain=REJOIN, data={"host": "old"})
    existing.add_to_hass(hass)

    body = await rejected_call_body(
        flow_tools.ha_set_integration(domain=REJOIN, config={"host": "new"})
    )

    assert body["status"] == "applied_but_unverified"
    assert "updated an existing config entry" in body["error"]["message"]
    assert dict(existing.data) == {"host": "new"}


async def test_an_entry_without_options_is_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    flow_tools: IntegrationTools,
) -> None:
    entry = MockConfigEntry(domain=INSTANT)
    entry.add_to_hass(hass)

    with flow_calls(core_client) as calls:
        body = await rejected_call_body(
            flow_tools.ha_set_integration(entry_id=entry.entry_id, config={})
        )

    assert "supports_options" in body["error"]["message"]
    calls["start_options_flow"].assert_not_called()
    assert hass.config_entries.options.async_progress() == []


# --- auto-backup wiring of ha_set_integration ---------------------------------


@pytest.mark.parametrize(
    ("call", "captures"), [("options", 1), ("abort", 0), ("log_level", 0)]
)
async def test_every_flow_call_but_an_abort_captures_a_backup(
    hass: HomeAssistant,
    flow_tools: IntegrationTools,
    hub: MockConfigEntry,
    backup_settings: config.Settings,
    call: str,
    captures: int,
) -> None:
    assert await async_setup_component(hass, "logger", {})
    snapshots = Path(backup_settings.auto_backup_dir)
    args: dict[str, Any] = {"log_level": "DEBUG"}
    if call == "options":
        args = {"config": {"string": "BAT"}}
    elif call == "abort":
        opened = await flow_tools.ha_set_integration(entry_id=hub.entry_id)
        args = {"flow_id": opened["flow_id"], "abort": True}
    before = snapshot_count(snapshots)

    result = await flow_tools.ha_set_integration(entry_id=hub.entry_id, **args)

    assert result["success"] is True
    assert snapshot_count(snapshots) - before == captures
