"""Driving a flow's ``progress`` step (#2632).

A progress step waits on work Home Assistant runs itself (a scan, a firmware
check). Core moves the flow on when that work is done; a GET on the flow
re-runs ``async_configure`` and answers the step the flow is at now, the same
call the frontend makes. The walkers wait for that step within a budget and
give up with Core's text for the progress action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import config_entry_flow_progress
from ha_mcp.tools.config_entry_flow_errors import FlowRef, ReconfigureStatus
from ha_mcp.tools.config_entry_flow_progress import (
    PROGRESS_BUDGET_S,
    wait_for_progress,
)
from ha_mcp.tools.config_entry_flow_walker import (
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
)
from ha_mcp.tools.config_entry_reconfigure import _run_reconfigure_flow

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
        client, flow, "f1", step, clock=clock, sleep=clock.sleep
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
async def test_progress_then_create_entry_returns_the_finished_flow() -> None:
    done = {"type": "create_entry", "flow_id": "f1", "result": {"entry_id": "e9"}}
    client = _client(dict(done))

    step = await _wait(client, dict(_PROGRESS), FlowRef("demo", "config"), _FakeClock())

    assert step == done


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
async def test_progress_done_is_moved_on_by_the_next_get() -> None:
    client = _client(dict(_FORM))

    step = await _wait(
        client,
        {"type": "progress_done", "flow_id": "f1", "step_id": "discover"},
        FlowRef("demo", "config"),
        _FakeClock(),
    )

    assert step == _FORM


@pytest.mark.asyncio
async def test_a_step_still_in_progress_after_the_budget_raises_with_cores_text() -> (
    None
):
    client = AsyncMock()
    client._request.return_value = dict(_PROGRESS)
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {
            "resources": {
                "component.demo.config.progress.scanning": "Scanning {count} hubs."
            }
        },
    }
    clock = _FakeClock()

    with pytest.raises(ToolError) as exc_info:
        await _wait(client, dict(_PROGRESS), FlowRef("demo", "config"), clock)

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "TIMEOUT_OPERATION"
    assert body["progress_action"] == "scanning"
    assert body["progress_action_text"] == "Scanning 3 hubs."
    assert body["flow_id"] == "f1"
    assert clock.now == pytest.approx(PROGRESS_BUDGET_S)


# --- the walkers drive a progress step instead of refusing it ---------------


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
async def test_reconfigure_progress_timeout_is_apply_failed_and_aborts_the_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_entry_flow_progress, "PROGRESS_BUDGET_S", 0.0)
    client = _client(strings={})
    client.start_reconfigure_flow.return_value = {
        **_PROGRESS,
        "flow_id": "flow-progress",
    }

    with pytest.raises(ToolError) as exc_info:
        await _run_reconfigure_flow(
            client,
            domain="demo",
            entry_id="e1",
            flow_config={"host": "h"},
            rollback_metadata={},
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "TIMEOUT_OPERATION"
    assert payload["status"] == ReconfigureStatus.APPLY_FAILED
    client.abort_config_flow.assert_awaited_once_with("flow-progress")
