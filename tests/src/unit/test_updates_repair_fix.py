"""A Repairs issue's fix flow, one step per ha_manage_updates(action='fix_repair') call.

Every flow is the test integration's own (``_repair_flows_core``), run by
Core's ``RepairsFlowManager``; the server's client drives it over Core's
fix-flow endpoints, as the Repairs dialog does.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import COMPONENT_INSTALL_HINT
from ha_mcp.tools.integration_flow_steps import reset_opened_flows
from ha_mcp.tools.tools_updates import UpdateTools

from ._repair_flows_core import (
    ABORT,
    CODE,
    CONFIRM,
    DOMAIN,
    FORM,
    HANDOFF,
    HANDOFF_ABORT,
    MENU,
    PROGRESS,
    STEP_DEVICE,
    STEP_PLACEHOLDER,
    SUCCESS_REASON_ABORT,
    UNFIXABLE,
    issue_exists,
    open_fix_flows,
)
from ._repair_flows_core import fix_tools as fix_tools
from ._tool_errors import rejected_call_body


def _item(issue_id: str, domain: str = DOMAIN) -> list[dict[str, Any]]:
    return [{"domain": domain, "issue_id": issue_id}]


async def _fix(tools: UpdateTools, issue_id: str, **call: Any) -> dict[str, Any]:
    return await tools.ha_manage_updates(
        action="fix_repair", repairs=_item(issue_id), **call
    )


async def test_the_first_call_shows_cores_step_and_answering_it_fixes_the_issue(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    """A fixable issue has no description of its own: the step's title and
    description, filled with the issue's placeholders, are what tells the
    caller what the fix does. Core deletes the issue once it is fixed."""
    step = await _fix(fix_tools, CONFIRM)

    assert step["step_id"] == "confirm"
    assert step["title"] == "Replace the kettle"
    assert step["description"] == "Select Submit once the kettle is replaced."
    assert step["fields"] == []
    assert list(open_fix_flows(hass)) == [step["flow_id"]]

    fixed = await _fix(fix_tools, CONFIRM, flow_id=step["flow_id"], config={})

    assert fixed["fixed"] is True
    assert fixed["create_entry_text"] == "The kettle works again."
    assert not issue_exists(hass, CONFIRM)
    assert open_fix_flows(hass) == {}


async def test_a_steps_own_placeholder_fills_its_text_over_the_issues(
    fix_tools: UpdateTools,
) -> None:
    """Where the step and the issue both fill a placeholder, the text shows
    the step's value, as the Repairs dialog does."""
    step = await _fix(fix_tools, STEP_PLACEHOLDER)

    assert step["title"] == f"Replace {STEP_DEVICE}"


async def test_a_refused_answer_keeps_the_flow_open_and_a_corrected_one_fixes(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    """The form's fields carry Core's help texts, filled with the issue's and
    the step's placeholders; Core's field error comes back with its text and
    the flow stays at the form for a corrected answer."""
    step = await _fix(fix_tools, FORM)
    assert step["fields"][0]["name"] == "code"
    assert step["fields"][0]["description"] == (
        "The code printed on the kettle, 4 digits long"
    )
    assert step["submit"] == "Check"
    # The component outlines config and options flows only.
    assert COMPONENT_INSTALL_HINT not in step["note"]

    body = await rejected_call_body(
        _fix(fix_tools, FORM, flow_id=step["flow_id"], config={"code": "0000"})
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["errors"] == {"code": "wrong_code"}
    assert body["error_text"] == {"code": "That code does not fit the kettle"}
    assert open_fix_flows(hass) == {step["flow_id"]: "code"}

    fixed = await _fix(fix_tools, FORM, flow_id=step["flow_id"], config={"code": CODE})

    assert fixed["fixed"] is True
    assert not issue_exists(hass, FORM)


async def test_a_menu_names_its_options_and_a_choice_answers_it(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    step = await _fix(fix_tools, MENU)

    assert step["menu_options"] == ["replace", "later"]
    assert step["menu_option_labels"] == {"replace": "Replace the kettle"}
    assert step["menu_option_descriptions"] == {"replace": "Order a new one"}

    fixed = await _fix(fix_tools, MENU, flow_id=step["flow_id"], menu_choice="replace")

    assert fixed["fixed"] is True
    assert not issue_exists(hass, MENU)


async def test_an_abort_by_home_assistant_gives_its_reason_and_keeps_the_issue(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    """The reason's text is the issue's own, filled with its placeholders,
    even when the abort names another translation domain."""
    body = await rejected_call_body(_fix(fix_tools, ABORT))

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert body["details"]["reason"] == "not_now"
    assert body["reason_text"] == "the kettle cannot be fixed now"
    assert issue_exists(hass, ABORT)


async def test_a_fix_flow_abort_is_never_read_as_an_updated_config_entry(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    """A reconfigure flow commits with reconfigure_successful; a fix flow
    that aborts with it changed no entry this call could check."""
    body = await rejected_call_body(_fix(fix_tools, SUCCESS_REASON_ABORT))

    assert body["details"]["reason"] == "reconfigure_successful"
    assert "status" not in body
    assert issue_exists(hass, SUCCESS_REASON_ABORT)


async def test_abort_ends_the_open_fix_flow_and_keeps_the_issue(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    step = await _fix(fix_tools, CONFIRM)

    ended = await _fix(fix_tools, CONFIRM, flow_id=step["flow_id"], abort=True)

    assert ended["aborted"] is True
    assert open_fix_flows(hass) == {}
    assert issue_exists(hass, CONFIRM)


@pytest.mark.parametrize(
    ("issue_id", "fixed"), [(HANDOFF, True), (HANDOFF_ABORT, False)]
)
async def test_a_fix_handed_over_to_another_flow_returns_that_flow(
    hass: HomeAssistant, fix_tools: UpdateTools, issue_id: str, fixed: bool
) -> None:
    """Core's next_flow is the fix continuing elsewhere, not a failure; Core
    keeps the issue when the fix flow itself aborted."""
    result = await _fix(fix_tools, issue_id)

    [setup] = hass.config_entries.flow.async_progress()
    assert result["fixed"] is fixed
    assert result["next_flow"] == ["config_flow", setup["flow_id"]]
    assert result["result"] is None
    assert issue_exists(hass, issue_id) is not fixed


async def test_a_progress_step_is_waited_out(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    step = await _fix(fix_tools, PROGRESS)

    assert step["step_id"] == "confirm"
    assert open_fix_flows(hass) == {step["flow_id"]: "confirm"}


@pytest.mark.parametrize("issue_id", [UNFIXABLE, "never_filed"])
async def test_an_issue_without_a_fix_flow_is_refused_by_home_assistant(
    hass: HomeAssistant, fix_tools: UpdateTools, issue_id: str
) -> None:
    """Core refuses to start a fix flow for an issue that is not fixable or
    that it does not hold; nothing opens."""
    body = await rejected_call_body(_fix(fix_tools, issue_id))

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert body["status_code"] == 400
    assert open_fix_flows(hass) == {}


async def test_a_flow_id_given_with_another_issue_is_refused_before_it_is_sent(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    """Core's flow is still at its step and no flow was opened for the other issue."""
    step = await _fix(fix_tools, CONFIRM)

    body = await rejected_call_body(
        _fix(fix_tools, FORM, flow_id=step["flow_id"], config={})
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert open_fix_flows(hass) == {step["flow_id"]: "confirm"}
    assert issue_exists(hass, CONFIRM)


@pytest.mark.parametrize(("issue_id", "continued"), [(CONFIRM, True), (FORM, False)])
async def test_a_flow_the_server_did_not_open_is_bound_by_cores_record(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    fix_tools: UpdateTools,
    issue_id: str,
    continued: bool,
) -> None:
    """After an ha-mcp restart the component reads which issue Core opened
    the flow for: it continues only with that issue."""
    manager = repairs_flow_manager(hass)
    assert manager is not None
    started = await manager.async_init(DOMAIN, context={"issue_id": CONFIRM})
    reset_opened_flows()
    call = _fix(fix_tools, issue_id, flow_id=started["flow_id"], config={})

    if continued:
        assert (await call)["fixed"] is True
    else:
        body = await rejected_call_body(call)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert open_fix_flows(hass) == {started["flow_id"]: "confirm"}


@pytest.mark.parametrize("flow_client", ["without_component"], indirect=True)
async def test_a_flow_the_server_did_not_open_is_refused_without_the_component(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    started = await manager.async_init(DOMAIN, context={"issue_id": CONFIRM})

    body = await rejected_call_body(
        _fix(fix_tools, CONFIRM, flow_id=started["flow_id"], config={})
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "ha_mcp_tools" in body["error"]["suggestion"]
    assert open_fix_flows(hass) == {started["flow_id"]: "confirm"}


async def test_a_fix_flow_of_ha_mcps_own_component_is_refused(
    hass: HomeAssistant, fix_tools: UpdateTools
) -> None:
    """Its fixes restart Home Assistant or replace the server's own token."""
    body = await rejected_call_body(
        fix_tools.ha_manage_updates(
            action="fix_repair",
            repairs=_item("server_token_needed", "ha_mcp_tools"),
        )
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "ha_mcp_tools" in body["error"]["message"]
    assert open_fix_flows(hass) == {}


@pytest.mark.parametrize(
    "repairs", [None, [], _item(CONFIRM) + _item(FORM), [{"domain": DOMAIN}]]
)
async def test_fix_repair_needs_exactly_one_issue(
    hass: HomeAssistant, fix_tools: UpdateTools, repairs: Any
) -> None:
    body = await rejected_call_body(
        fix_tools.ha_manage_updates(action="fix_repair", repairs=repairs)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert open_fix_flows(hass) == {}
