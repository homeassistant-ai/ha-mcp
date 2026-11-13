"""Driving a flow's ``progress`` step (#2632): a GET on the flow answers its current step.

Every flow below is Core's own, on a real ``hass``: its progress task, its
strings and its answers to the server's client. Only a lost answer (the
client hearing nothing back) is made in the test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import anyio
import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, mock_config_flow

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import config_entry_flow_progress
from ha_mcp.tools.config_entry_flow import CreationFlowError, recreate_flow_helper
from ha_mcp.tools.config_entry_flow_errors import ReconfigureStatus
from ha_mcp.tools.config_entry_flow_introspect import (
    abort_failed_flow,
    abort_failed_flow_type_only,
)
from ha_mcp.tools.integration_flow_steps import FlowTarget, drive_flow

from ._mock_flow_integration import (
    answer_translations,
    mock_flow_integration,
    open_flow_ids,
)
from ._tool_errors import error_body

_DOMAIN = "flow_progress_probe"
_SCANNING = {"progress": {"scanning": "Scanning {count} hubs."}}


@dataclass
class _Run:
    """What the probe's flows do in one test."""

    #: ``form``: a form; ``progress``: a progress step until ``release``;
    #: ``crash``: a form whose submit raises inside Core.
    mode: str = "form"
    #: After the progress step, the setup flow shows a form or creates the entry.
    after: str = "create"
    release: asyncio.Event = field(default_factory=asyncio.Event)


class _Probe:
    run: ClassVar[_Run] = _Run()


def _form(flow: Any, step_id: str, stored: Mapping[str, Any], **kwargs: Any) -> Any:
    schema = probatio.Schema(
        {
            probatio.Optional("host"): selector.TextSelector(),
            probatio.Optional("api_key"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )
    return flow.async_show_form(
        step_id=step_id,
        data_schema=flow.add_suggested_values_to_schema(schema, stored),
        **kwargs,
    )


async def _work(release: asyncio.Event) -> None:
    await asyncio.sleep(0)  # never done before the step returns
    await release.wait()


async def _first_step(
    flow: Any, step_id: str, stored: Mapping[str, Any], user_input: Any
) -> Any:
    """Every flow kind's first step, as ``_Probe.run`` says."""
    run = _Probe.run
    if run.mode == "progress":
        if flow.task is None:
            flow.task = flow.hass.async_create_task(_work(run.release))
        if not flow.task.done():
            return flow.async_show_progress(
                step_id=step_id,
                progress_action="scanning",
                description_placeholders={"count": "3"},
                progress_task=flow.task,
            )
        return flow.async_show_progress_done(next_step_id="finish")
    if user_input is not None:
        if run.mode == "crash":
            raise RuntimeError("the step failed inside Home Assistant")
        if user_input.get("host") == "refuse":
            return _form(flow, step_id, stored, errors={"host": "invalid"})
        if user_input.get("host") == "abort":
            return flow.async_abort(reason="gone")
    return _form(flow, step_id, stored)


class _OptionsFlow(OptionsFlow):
    task: asyncio.Task[None] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await _first_step(self, "init", self.config_entry.options, user_input)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_create_entry(data={"scanned": True})


class _PlaneFlow(ConfigSubentryFlow):
    task: asyncio.Task[None] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await _first_step(self, "user", {}, user_input)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_create_entry(title="Plane", data={})


class _ConfigFlow(ConfigFlow):
    VERSION = 1
    task: asyncio.Task[None] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await _first_step(self, "user", {}, user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        stored = self._get_reconfigure_entry().data
        return await _first_step(self, "reconfigure", stored, user_input)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if _Probe.run.after == "form":
            return await self.async_step_pick()
        return self.async_create_entry(title="Probe", data={})

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return _form(self, "pick", {})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _OptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"plane": _PlaneFlow}


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> _Run:
    fresh = _Run()
    monkeypatch.setattr(_Probe, "run", fresh)
    return fresh


@pytest.fixture
def probe(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    mock_flow_integration(
        hass,
        _DOMAIN,
        tmp_path,
        monkeypatch,
        {
            "config": _SCANNING,
            "options": _SCANNING,
            "config_subentries": {"plane": _SCANNING},
        },
    )
    with mock_config_flow(_DOMAIN, _ConfigFlow):
        yield


@pytest.fixture
async def client(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    probe: None,
    run: _Run,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client on a ``hass`` serving the probe's flows and strings.

    The poll starts at once rather than after its first second.
    """
    monkeypatch.setattr(config_entry_flow_progress, "POLL_INTERVAL_S", 0.0)
    assert await async_setup_component(hass, "config", {})
    answer_translations(hass)
    yield core_client
    # A progress task still waiting ends, and Core moves its flow on.
    run.release.set()
    await hass.async_block_till_done()


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    config_entry = MockConfigEntry(
        domain=_DOMAIN,
        data={"host": "10.0.0.1"},
        options={"host": "10.0.0.1", "api_key": "sk-LIVE"},
    )
    config_entry.add_to_hass(hass)
    return config_entry


_KINDS = ["setup", "options", "subentry", "reconfigure"]
_ABORT_FN = {
    "setup": "abort_config_flow",
    "options": "abort_options_flow",
    "subentry": "abort_config_subentry_flow",
    "reconfigure": "abort_config_flow",
}
_SUBMIT_FN = {
    "setup": "submit_config_flow_step",
    "options": "submit_options_flow_step",
    "subentry": "submit_config_subentry_flow_step",
    "reconfigure": "submit_config_flow_step",
}


def _target(
    client: HomeAssistantClient, kind: str, entry: MockConfigEntry
) -> tuple[FlowTarget, Callable[[], Awaitable[dict[str, Any]]]]:
    """The flow of ``kind`` and the client call that opens it in Core."""
    if kind == "setup":
        return FlowTarget("setup", _DOMAIN), partial(client.start_config_flow, _DOMAIN)
    if kind == "options":
        return (
            FlowTarget("options", _DOMAIN, entry.entry_id),
            partial(client.start_options_flow, entry.entry_id),
        )
    if kind == "subentry":
        return (
            FlowTarget("subentry", _DOMAIN, entry.entry_id, subentry_type="plane"),
            partial(client.start_config_subentry_flow, entry.entry_id, "plane"),
        )
    return (
        FlowTarget("reconfigure", _DOMAIN, entry.entry_id),
        partial(client.start_reconfigure_flow, _DOMAIN, entry.entry_id),
    )


async def _drive(
    client: HomeAssistantClient,
    target: FlowTarget,
    start: Callable[[], Awaitable[dict[str, Any]]],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One step-driver call that opens the flow."""
    return await drive_flow(
        client,
        target,
        flow_id=None,
        config=config,
        menu_choice=None,
        abort=False,
        start=start,
    )


@pytest.mark.parametrize("kind", ["setup", "options", "subentry"])
async def test_a_step_driver_call_waits_out_a_progress_step(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    run: _Run,
    entry: MockConfigEntry,
    kind: str,
) -> None:
    """Each flow kind is polled on its own resource until Core finishes it."""
    run.mode = "progress"
    run.release.set()
    target, start = _target(client, kind, entry)

    result = await _drive(client, target, start)

    assert result["success"] is True
    assert open_flow_ids(hass) == []
    if kind == "setup":
        (created,) = [
            e for e in hass.config_entries.async_entries(_DOMAIN) if e is not entry
        ]
        assert result["entry_id"] == created.entry_id
    elif kind == "options":
        assert entry.options == {"scanned": True}
    else:
        assert [s.title for s in entry.subentries.values()] == ["Plane"]


async def test_progress_then_form_returns_the_form_from_a_get_on_the_flow(
    hass: HomeAssistant, client: HomeAssistantClient, run: _Run
) -> None:
    run.mode, run.after = "progress", "form"
    run.release.set()

    result = await _drive(
        client,
        FlowTarget("setup", _DOMAIN),
        partial(client.start_config_flow, _DOMAIN),
    )

    assert (result["flow_type"], result["step_id"]) == ("form", "pick")
    assert open_flow_ids(hass) == [result["flow_id"]]


@pytest.mark.parametrize("kind", _KINDS)
async def test_a_step_out_of_progress_budget_keeps_the_flow_open_with_cores_text(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    run: _Run,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(config_entry_flow_progress, "PROGRESS_BUDGET_S", 0.0)
    run.mode = "progress"
    target, start = _target(client, kind, entry)
    abort = _ABORT_FN[kind]

    with (
        patch.object(client, abort, wraps=getattr(client, abort)) as aborted,
        pytest.raises(ToolError) as exc_info,
    ):
        await _drive(client, target, start)

    body = error_body(exc_info)
    assert body["error"]["code"] == "TIMEOUT_OPERATION"
    assert body["progress_action"] == "scanning"
    assert body["progress_action_text"] == "Scanning 3 hubs."
    assert open_flow_ids(hass) == [body["flow_id"]]
    aborted.assert_not_awaited()


def _manager(hass: HomeAssistant, kind: str) -> Any:
    if kind == "options":
        return hass.config_entries.options
    if kind == "subentry":
        return hass.config_entries.subentries
    return hass.config_entries.flow


# A lost answer to the poll (the GET finishes the flow past progress_done) may
# have committed: applied_but_unverified. Core answers 404 to the client's
# retry after a gateway 5xx when the first GET finished the flow.
@pytest.mark.parametrize("answer", ["none", "flow-gone"])
@pytest.mark.parametrize("kind", _KINDS)
async def test_a_lost_poll_answer_is_possibly_applied_in_every_flow_kind(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    run: _Run,
    entry: MockConfigEntry,
    kind: str,
    answer: str,
) -> None:
    run.mode = "progress"
    target, start = _target(client, kind, entry)
    abort = _ABORT_FN[kind]

    with ExitStack() as stack:

        async def start_then_lose_the_poll() -> dict[str, Any]:
            step = await start()
            if answer == "none":
                stack.enter_context(
                    patch.object(
                        client,
                        "_request",
                        AsyncMock(side_effect=HomeAssistantConnectionError("reset")),
                    )
                )
            else:
                _manager(hass, kind).async_abort(step["flow_id"])
            return step

        aborted = stack.enter_context(
            patch.object(client, abort, wraps=getattr(client, abort))
        )
        with pytest.raises(ToolError) as exc_info:
            await _drive(client, target, start_then_lose_the_poll)

    assert error_body(exc_info)["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    aborted.assert_not_called()


@pytest.mark.parametrize("kind", _KINDS)
async def test_a_lost_submit_answer_is_possibly_applied_in_every_flow_kind(
    client: HomeAssistantClient, entry: MockConfigEntry, kind: str
) -> None:
    target, start = _target(client, kind, entry)
    submit, abort = _SUBMIT_FN[kind], _ABORT_FN[kind]

    with (
        patch.object(
            client,
            submit,
            AsyncMock(side_effect=HomeAssistantConnectionError("reset")),
        ) as submitted,
        patch.object(client, abort, wraps=getattr(client, abort)) as aborted,
        pytest.raises(ToolError) as exc_info,
    ):
        await _drive(client, target, start, {"host": "10.0.0.2"})

    assert error_body(exc_info)["status"] == ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    submitted.assert_awaited_once()
    aborted.assert_not_called()


@pytest.mark.usefixtures("redact_on")
async def test_a_lost_submit_answer_redacts_the_forms_password_value(
    client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    # The options form carries the stored api_key in suggested_value.
    target, start = _target(client, "options", entry)

    with (
        patch.object(
            client,
            "submit_options_flow_step",
            AsyncMock(side_effect=HomeAssistantConnectionError("reset")),
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await _drive(client, target, start, {"host": "10.0.0.2"})

    assert "sk-LIVE" not in str(exc_info.value)
    schema = error_body(exc_info)["details"]["data_schema"]
    assert "api_key" in [f["name"] for f in schema]


@pytest.mark.parametrize("kind", _KINDS)
async def test_a_500_leaves_only_a_reconfigure_possibly_applied(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    run: _Run,
    entry: MockConfigEntry,
    kind: str,
) -> None:
    # In Core's flows only a reconfigure step writes the entry before it returns.
    run.mode = "crash"
    target, start = _target(client, kind, entry)
    submit, abort = _SUBMIT_FN[kind], _ABORT_FN[kind]
    reconfigure = kind == "reconfigure"

    with (
        patch.object(client, submit, wraps=getattr(client, submit)) as submitted,
        patch.object(client, abort, wraps=getattr(client, abort)) as aborted,
        pytest.raises(ToolError if reconfigure else HomeAssistantAPIError) as exc_info,
    ):
        await _drive(client, target, start, {"host": "10.0.0.2"})

    submitted.assert_awaited_once()
    if reconfigure:
        assert json.loads(str(exc_info.value))["status"] == (
            ReconfigureStatus.APPLIED_BUT_UNVERIFIED
        )
    else:
        assert getattr(exc_info.value, "status_code", None) == 500
    # Either way the flow is the caller's to answer again or abort.
    aborted.assert_not_called()
    assert len(open_flow_ids(hass)) == 1


# An anyio cancel scope re-delivers cancellation at every later await, so the
# abort must be shielded to reach Home Assistant.
@pytest.mark.parametrize(
    "abort_after_error",
    [
        partial(abort_failed_flow, kind="flow"),
        # The helper recreation cleanup's abort.
        partial(abort_failed_flow_type_only, msg="cleanup failed (%s)"),
    ],
    ids=["failed-flow", "helper-recreation"],
)
@pytest.mark.parametrize("kind", ["setup", "subentry"])
async def test_best_effort_abort_is_sent_inside_a_cancelled_scope(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    kind: str,
    abort_after_error: Callable[..., Awaitable[None]],
) -> None:
    target, start = _target(client, kind, entry)
    flow_id = (await start())["flow_id"]

    with anyio.CancelScope() as scope:
        scope.cancel()
        await abort_after_error(target.abort_fn(client), flow_id)

    assert open_flow_ids(hass) == []


async def test_a_failed_abort_after_an_error_logs_its_traceback(
    client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(
            client,
            "abort_config_subentry_flow",
            AsyncMock(side_effect=HomeAssistantConnectionError("ws down")),
        ),
        caplog.at_level("WARNING"),
    ):
        await abort_failed_flow(
            client.abort_config_subentry_flow, "f1", kind="config subentry flow"
        )

    (record,) = [r for r in caplog.records if "f1" in r.getMessage()]
    assert record.getMessage().startswith(
        "Failed to abort config subentry flow f1 after error"
    )
    assert record.exc_info is not None


@pytest.mark.parametrize(
    ("host", "reason", "level", "error_type"),
    [
        # The refused form is still open; its abort gets no answer.
        ("refuse", "validation_failed", "WARNING", "HomeAssistantConnectionError"),
        # Core already ended the flow: its abort meets Core's 404.
        ("abort", "flow_aborted", "DEBUG", "HomeAssistantAPIError"),
    ],
    ids=["failed", "gone"],
)
async def test_a_helper_recreation_abort_logs_the_error_type_only(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
    host: str,
    reason: str,
    level: str,
    error_type: str,
) -> None:
    # The error's text may echo the snapshot's values.
    with ExitStack() as stack:
        if host == "refuse":
            stack.enter_context(
                patch.object(
                    client,
                    "abort_config_flow",
                    AsyncMock(
                        side_effect=HomeAssistantConnectionError("PRIVATE_TEST_PAYLOAD")
                    ),
                )
            )
        stack.enter_context(caplog.at_level(logging.DEBUG))
        with pytest.raises(CreationFlowError):
            await recreate_flow_helper(client, _DOMAIN, {"host": host})

    (record,) = [r for r in caplog.records if "cleanup failed" in r.getMessage()]
    assert record.levelname == level
    assert re.fullmatch(
        rf"Helper recreation flow \w+ cleanup failed \(reason={reason}, "
        rf"error_type={error_type}\)",
        record.getMessage(),
    )
    assert "PRIVATE_TEST_PAYLOAD" not in caplog.text
