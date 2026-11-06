"""Driving a flow's ``progress`` step (#2632): a GET on the flow answers its current step."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import config_entry_flow_progress
from ha_mcp.tools.config_entry_flow import (
    OptionsFlowError,
    _cleanup_snapshot_creation,
    _CreationFlowProgress,
    create_config_entry,
    get_user_step_field_names,
    set_config_subentry,
    update_config_entry_options,
)
from ha_mcp.tools.config_entry_flow_errors import FlowRef, ReconfigureStatus
from ha_mcp.tools.config_entry_flow_introspect import abort_failed_flow
from ha_mcp.tools.config_entry_flow_progress import (
    PROGRESS_BUDGET_S,
    wait_for_progress,
)
from ha_mcp.tools.config_entry_flow_walker import (
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
    leaves_flow_open,
)
from ha_mcp.tools.integration_flow_steps import FlowTarget, drive_flow

_PROGRESS = {
    "type": "progress",
    "flow_id": "f1",
    "step_id": "discover",
    "progress_action": "scanning",
    "description_placeholders": {"count": "3"},
}
_FORM = {"type": "form", "flow_id": "f1", "step_id": "pick", "data_schema": []}


@dataclass
class _FakeClock:
    """A monotonic clock that only moves when the poll sleeps."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(*polled: dict[str, Any], strings: dict[str, str] | None = None):
    client = AsyncMock()
    client._request.side_effect = list(polled)
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {"resources": strings or {}},
    }
    return client


async def _wait(client: Any, step: dict[str, Any], flow: FlowRef, clock: _FakeClock):
    return await wait_for_progress(
        client,
        flow,
        "f1",
        step,
        poll=lambda url, _step: client._request("GET", url),
        clock=clock,
        sleep=clock.sleep,
    )


@pytest.mark.asyncio
async def test_progress_then_form_returns_the_form_from_a_get_on_the_flow() -> None:
    client = _client(dict(_PROGRESS), dict(_FORM))
    clock = _FakeClock()

    step = await _wait(client, dict(_PROGRESS), FlowRef("demo", "config"), clock)

    assert step == _FORM
    assert [c.args for c in client._request.await_args_list] == [
        ("GET", "/config/config_entries/flow/f1"),
        ("GET", "/config/config_entries/flow/f1"),
    ]
    assert clock.sleeps and all(s > 0 for s in clock.sleeps)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "polled"),
    [
        (_PROGRESS, {"type": "create_entry", "flow_id": "f1", "result": {}}),
        ({"type": "progress_done", "flow_id": "f1", "step_id": "d"}, _FORM),
    ],
    ids=["create_entry", "progress_done"],
)
async def test_progress_is_moved_on_by_the_next_get(
    first: dict[str, Any], polled: dict[str, Any]
) -> None:
    client = _client(dict(polled))

    step = await _wait(client, dict(first), FlowRef("demo", "config"), _FakeClock())

    assert step == polled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "resource"),
    [
        (FlowRef("demo", "options"), "options/flow"),
        (FlowRef(None, "config_subentries.conversation", "e1"), "subentries/flow"),
    ],
)
async def test_each_flow_kind_is_polled_on_its_own_resource(
    flow: FlowRef, resource: str
) -> None:
    client = _client(dict(_FORM))

    await _wait(client, dict(_PROGRESS), flow, _FakeClock())

    client._request.assert_awaited_once_with(
        "GET", f"/config/config_entries/{resource}/f1"
    )


@pytest.mark.asyncio
async def test_a_step_still_in_progress_after_the_budget_raises_with_cores_text() -> (
    None
):
    client = _client(
        strings={"component.demo.config.progress.scanning": "Scanning {count} hubs."}
    )
    client._request.side_effect = None
    client._request.return_value = dict(_PROGRESS)
    clock = _FakeClock()

    with pytest.raises(ToolError) as exc_info:
        await _wait(client, dict(_PROGRESS), FlowRef("demo", "config"), clock)

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "TIMEOUT_OPERATION"
    assert body["progress_action"] == "scanning"
    assert body["progress_action_text"] == "Scanning 3 hubs."
    assert body["flow_id"] == "f1"
    assert clock.now == pytest.approx(PROGRESS_BUDGET_S)


@pytest.fixture
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_entry_flow_progress, "POLL_INTERVAL_S", 0.0)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_wait")
async def test_config_flow_walker_waits_out_a_progress_step() -> None:
    created = {"type": "create_entry", "flow_id": "f1", "result": {"entry_id": "e9"}}
    client = _client(dict(created))

    result = await _handle_flow_steps(
        client, "f1", dict(_PROGRESS), {}, flow=FlowRef("demo", "config")
    )

    assert result["entry"] == created
    client._request.assert_awaited_once_with("GET", "/config/config_entries/flow/f1")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_wait")
async def test_subentry_walker_waits_out_a_progress_step() -> None:
    created = {"type": "create_entry", "flow_id": "f1", "result": {}}
    client = _client(dict(created))

    result = await _handle_config_subentry_flow_steps(
        client,
        "f1",
        dict(_PROGRESS),
        {},
        is_reconfigure=False,
        flow=FlowRef(None, "config_subentries.conversation", "e1"),
    )

    assert result["operation"] == "created"
    client._request.assert_awaited_once_with(
        "GET", "/config/config_entries/subentries/flow/f1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("step_type", ["external", "external_done"])
async def test_external_steps_are_still_refused(step_type: str) -> None:
    client = _client()

    with pytest.raises(ToolError) as exc_info:
        await _handle_flow_steps(
            client,
            "f1",
            {"type": step_type, "flow_id": "f1", "step_id": "auth"},
            {},
            flow=FlowRef("demo", "config"),
        )

    assert "cannot be completed via MCP" in str(exc_info.value)
    client._request.assert_not_called()


@pytest.mark.asyncio
async def test_reconfigure_progress_timeout_keeps_the_flow_open_and_carries_flow_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_entry_flow_progress, "PROGRESS_BUDGET_S", 0.0)
    client = _client(strings={})
    client.start_reconfigure_flow.return_value = {
        **_PROGRESS,
        "flow_id": "flow-progress",
    }

    with pytest.raises(ToolError) as exc_info:
        await drive_flow(
            client,
            FlowTarget("reconfigure", "demo", "e1"),
            flow_id=None,
            config={"host": "h"},
            menu_choice=None,
            abort=False,
            start=lambda: client.start_reconfigure_flow("demo", "e1"),
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "TIMEOUT_OPERATION"
    assert payload["flow_id"] == "flow-progress"
    client.abort_config_flow.assert_not_awaited()


# A lost answer to the poll (the GET finishes the flow past progress_done) or to
# a submit may have committed: applied_but_unverified.
_NO_ANSWERS = [
    pytest.param(TimeoutError(), id="timeout"),
    pytest.param(HomeAssistantConnectionError("reset"), id="connection"),
    pytest.param(
        HomeAssistantAPIError("API error: 504", status_code=504), id="http-504"
    ),
]
# The client's retry after a gateway 5xx meets this 404 when the GET finished the flow.
_POLL_404 = pytest.param(
    HomeAssistantAPIError("API error: 404 - Invalid flow specified", status_code=404),
    id="http-404",
)


async def _create_entry(client: AsyncMock, step: dict[str, Any]) -> Any:
    client.start_config_flow.return_value = step
    return await create_config_entry(client, "demo", {})


async def _update_options(client: AsyncMock, step: dict[str, Any]) -> Any:
    client.get_config_entry.return_value = {"entry_id": "e1", "domain": "demo"}
    client.start_options_flow.return_value = step
    return await update_config_entry_options(client, "e1", {})


async def _create_subentry(client: AsyncMock, step: dict[str, Any]) -> Any:
    client.start_config_subentry_flow.return_value = step
    return await set_config_subentry(client, "e1", "plane", {})


_ABORT_ANSWERS = [
    pytest.param(None, id="aborted"),
    pytest.param(HomeAssistantAPIError("API error: 404", status_code=404), id="gone"),
    pytest.param(
        HomeAssistantAPIError("API error: 500", status_code=500), id="http-500"
    ),
    pytest.param(TimeoutError(), id="timeout"),
]
_WRITES = {
    "create": (_create_entry, "submit_config_flow_step", "abort_config_flow"),
    "subentry": (
        _create_subentry,
        "submit_config_subentry_flow_step",
        "abort_config_subentry_flow",
    ),
    "options": (_update_options, "submit_options_flow_step", "abort_options_flow"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("run", list(_WRITES))
@pytest.mark.parametrize("abort_error", _ABORT_ANSWERS)
async def test_a_write_out_of_progress_budget_closes_its_flow(
    monkeypatch: pytest.MonkeyPatch, run: str, abort_error: Exception | None
) -> None:
    monkeypatch.setattr(config_entry_flow_progress, "PROGRESS_BUDGET_S", 0.0)
    client = _client(strings={})
    client.get_config_entry.return_value = {"entry_id": "e1", "domain": "demo"}
    write, _, abort = _WRITES[run]
    getattr(client, abort).side_effect = abort_error

    with pytest.raises(ToolError) as exc_info:
        await write(client, dict(_PROGRESS))

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "TIMEOUT_OPERATION"
    assert "warnings" not in payload
    getattr(client, abort).assert_awaited_once_with("f1")


@pytest.mark.asyncio
@pytest.mark.parametrize("run", list(_WRITES))
@pytest.mark.parametrize("abort_error", _ABORT_ANSWERS)
@pytest.mark.parametrize("cancelled", [False, True], ids=["answered", "cancelled"])
async def test_a_failed_write_closes_its_flow_and_keeps_its_error(
    run: str, abort_error: Exception | None, cancelled: bool
) -> None:
    # An answered submit error counts as not committed: abort, re-raise as raised.
    client = _client(strings={})
    write, submit, abort = _WRITES[run]
    getattr(client, submit).side_effect = (
        asyncio.CancelledError()
        if cancelled
        else HomeAssistantAPIError("API error: 500", status_code=500)
    )
    getattr(client, abort).side_effect = abort_error

    with pytest.raises(asyncio.CancelledError if cancelled else HomeAssistantAPIError):
        await write(client, dict(_FORM))

    getattr(client, abort).assert_awaited_once_with("f1")


async def _reconfigure(client: AsyncMock, step: dict[str, Any]) -> Any:
    client.start_reconfigure_flow.return_value = step
    return await drive_flow(
        client,
        FlowTarget("reconfigure", "demo", "e1"),
        flow_id=None,
        config={},
        menu_choice=None,
        abort=False,
        start=lambda: client.start_reconfigure_flow("demo", "e1"),
    )


_FLOW_KINDS = [
    pytest.param(
        _create_entry, "submit_config_flow_step", "abort_config_flow", id="create"
    ),
    pytest.param(
        _update_options, "submit_options_flow_step", "abort_options_flow", id="options"
    ),
    pytest.param(
        _create_subentry,
        "submit_config_subentry_flow_step",
        "abort_config_subentry_flow",
        id="subentry",
    ),
    pytest.param(
        _reconfigure, "submit_config_flow_step", "abort_config_flow", id="reconfigure"
    ),
]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_wait")
@pytest.mark.parametrize("no_answer", [*_NO_ANSWERS, _POLL_404])
@pytest.mark.parametrize(("run", "submit", "abort"), _FLOW_KINDS)
async def test_a_lost_poll_answer_is_possibly_applied_in_every_flow_kind(
    no_answer: Exception, run: Any, submit: str, abort: str
) -> None:
    client = _client(strings={})
    client._request.side_effect = no_answer

    with pytest.raises(ToolError) as exc_info:
        await run(client, dict(_PROGRESS))

    assert json.loads(str(exc_info.value))["status"] == (
        ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    )
    getattr(client, abort).assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("no_answer", _NO_ANSWERS)
@pytest.mark.parametrize(("run", "submit", "abort"), _FLOW_KINDS)
async def test_a_lost_submit_answer_is_possibly_applied_in_every_flow_kind(
    no_answer: Exception, run: Any, submit: str, abort: str
) -> None:
    client = _client(strings={})
    getattr(client, submit).side_effect = no_answer

    with pytest.raises(ToolError) as exc_info:
        await run(client, dict(_FORM))

    assert json.loads(str(exc_info.value))["status"] == (
        ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    )
    getattr(client, submit).assert_awaited_once()
    getattr(client, abort).assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "submit", "abort"), _FLOW_KINDS)
async def test_a_lost_submit_answer_redacts_the_forms_password_value(
    monkeypatch: pytest.MonkeyPatch, run: Any, submit: str, abort: str
) -> None:
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=True, redact_app_option_keys=""),
    )
    secret = {
        "name": "api_key",
        "selector": {"text": {"type": "password"}},
        "description": {"suggested_value": "sk-LIVE"},
    }
    client = _client(strings={})
    getattr(client, submit).side_effect = TimeoutError()

    with pytest.raises(ToolError) as exc_info:
        await run(client, {**_FORM, "data_schema": [secret]})

    assert "sk-LIVE" not in str(exc_info.value)
    details = json.loads(str(exc_info.value))["details"]
    assert details["data_schema"][0]["name"] == "api_key"


@pytest.mark.asyncio
@pytest.mark.parametrize(("run", "submit", "abort"), _FLOW_KINDS)
async def test_a_500_leaves_only_a_reconfigure_possibly_applied(
    run: Any, submit: str, abort: str
) -> None:
    # In Core's flows only a reconfigure step writes the entry before it returns.
    client = _client(strings={})
    getattr(client, submit).side_effect = HomeAssistantAPIError(
        "API error: 500", status_code=500
    )
    reconfigure = run is _reconfigure

    with pytest.raises(ToolError if reconfigure else HomeAssistantAPIError) as exc_info:
        await run(client, dict(_FORM))

    getattr(client, submit).assert_awaited_once()
    assert leaves_flow_open(exc_info.value) is reconfigure
    assert getattr(client, abort).called is not reconfigure


def test_only_a_flow_tool_error_can_leave_the_flow_open() -> None:
    text = json.dumps({"status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED})

    assert leaves_flow_open(ToolError(text))
    assert not leaves_flow_open(RuntimeError(text))
    assert not leaves_flow_open(ToolError("not json"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_wait")
@pytest.mark.parametrize("run", list(_WRITES))
async def test_a_write_cancelled_during_the_wait_aborts_the_flow(run: str) -> None:
    client = _client(strings={})
    client._request.side_effect = asyncio.CancelledError()
    write, _, abort = _WRITES[run]

    with pytest.raises(asyncio.CancelledError):
        await write(client, dict(_PROGRESS))

    getattr(client, abort).assert_awaited_once_with("f1")


# An anyio cancel scope re-delivers cancellation at every later await, so the
# abort must be shielded to reach Home Assistant.


def _abort_recorder() -> tuple[list[str], Any]:
    sent: list[str] = []

    async def abort(flow_id: str) -> None:
        await asyncio.sleep(0)
        sent.append(flow_id)

    return sent, abort


async def _cancel_scope_at_poll(client: AsyncMock, run: Any) -> None:
    with anyio.CancelScope() as scope:

        async def poll(*_args: Any, **_kwargs: Any) -> None:
            scope.cancel()
            await asyncio.sleep(0)

        client._request.side_effect = poll
        await run()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_wait")
@pytest.mark.parametrize("run", list(_WRITES))
async def test_a_write_in_a_cancel_scope_still_sends_the_abort(run: str) -> None:
    client = _client(strings={})
    write, _, abort = _WRITES[run]
    sent, getattr(client, abort).side_effect = _abort_recorder()

    await _cancel_scope_at_poll(client, lambda: write(client, dict(_PROGRESS)))

    assert sent == ["f1"]


@pytest.mark.asyncio
async def test_helper_field_introspection_sends_its_abort_inside_a_cancelled_scope() -> (
    None
):
    client = AsyncMock()
    sent, client.abort_config_flow.side_effect = _abort_recorder()

    with anyio.CancelScope() as scope:

        async def start(_helper_type: str) -> dict[str, Any]:
            scope.cancel()
            return {"flow_id": "f1", "type": "form", "data_schema": []}

        client.start_config_flow.side_effect = start
        await get_user_step_field_names(client, "switch_as_x")

    assert sent == ["f1"]


@pytest.mark.asyncio
async def test_best_effort_abort_is_sent_inside_a_cancelled_scope() -> None:
    client = AsyncMock()
    sent, client.abort_config_flow.side_effect = _abort_recorder()
    sent_sub, client.abort_config_subentry_flow.side_effect = _abort_recorder()

    with anyio.CancelScope() as scope:
        scope.cancel()
        await abort_failed_flow(client.abort_config_flow, "f1")
        await abort_failed_flow(client.abort_config_subentry_flow, "s1")

    assert (sent, sent_sub) == (["f1"], ["s1"])


@pytest.mark.asyncio
async def test_helper_recreation_cleanup_is_sent_inside_a_cancelled_scope() -> None:
    client = AsyncMock()
    sent, client.abort_config_flow.side_effect = _abort_recorder()
    progress = _CreationFlowProgress({})
    progress.flow_id = "f1"
    failure = OptionsFlowError("refused", apply_status="not_applied", entry_id="")

    with anyio.CancelScope() as scope:
        scope.cancel()
        await _cleanup_snapshot_creation(client, progress, failure)

    assert sent == ["f1"]


@pytest.mark.asyncio
async def test_a_failed_abort_after_an_error_logs_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    abort = AsyncMock(side_effect=RuntimeError("ws down"))

    with caplog.at_level("WARNING"):
        await abort_failed_flow(abort, "f1", kind="config subentry flow")

    (record,) = [r for r in caplog.records if "f1" in r.getMessage()]
    assert record.getMessage().startswith(
        "Failed to abort config subentry flow f1 after error"
    )
    assert record.exc_info is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("abort_error", "level"),
    [
        pytest.param(RuntimeError("PRIVATE_TEST_PAYLOAD"), "WARNING", id="failed"),
        pytest.param(
            HomeAssistantAPIError("PRIVATE_TEST_PAYLOAD", status_code=404),
            "DEBUG",
            id="gone",
        ),
    ],
)
async def test_a_helper_recreation_abort_logs_the_error_type_only(
    caplog: pytest.LogCaptureFixture, abort_error: Exception, level: str
) -> None:
    # The error's text may echo the snapshot's values.
    client = AsyncMock()
    client.abort_config_flow.side_effect = abort_error
    progress = _CreationFlowProgress({})
    progress.flow_id = "f1"
    failure = OptionsFlowError(
        "refused", apply_status="not_applied", entry_id="", reason="flow_aborted"
    )

    with caplog.at_level("DEBUG"):
        await _cleanup_snapshot_creation(client, progress, failure)

    (record,) = [r for r in caplog.records if "f1" in r.getMessage()]
    assert record.levelname == level
    assert record.getMessage() == (
        "Helper recreation flow f1 cleanup failed (reason=flow_aborted, "
        f"error_type={type(abort_error).__name__})"
    )
    assert "PRIVATE_TEST_PAYLOAD" not in caplog.text
