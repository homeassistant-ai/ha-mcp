"""Unit tests for the component-side ``ha_mcp_tools/logbook_events`` command.

The command runs Core's own ``EventProcessor`` (with the REST flags: ISO
``when`` values, ``include_entity_name=True``) off the event loop and mirrors
Core's ``logbook/get_events`` validation. These tests pin the parts the
component owns — validation, the filter combination rules, the processor
flags, and the executor hand-off — against a stubbed HA.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _StubHomeAssistantError(Exception):
    """Stand-in for core's ``HomeAssistantError`` in the stubbed env."""


# Stub the HA modules the component surface imports (the same MagicMock pattern
# the sibling component test modules install); ``_component_ws_api`` adds the
# logbook/recorder stubs on top.
for _mod in (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.config",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.storage",
    "homeassistant.loader",
):
    if _mod == "homeassistant.exceptions":
        _exceptions = MagicMock()
        _exceptions.HomeAssistantError = _StubHomeAssistantError
        sys.modules.setdefault(_mod, _exceptions)
    else:
        sys.modules.setdefault(_mod, MagicMock())

_exceptions = MagicMock()
_exceptions.HomeAssistantError = _StubHomeAssistantError
sys.modules.setdefault("homeassistant.exceptions", _exceptions)

from custom_components.ha_mcp_tools.websocket_api import (  # noqa: E402
    logbook as component_logbook,
)

from ._component_ws_api import wsapi  # noqa: E402,F401  (installs stubs, loads surface)


def _dt_module() -> ModuleType:
    """A minimal real ``homeassistant.util.dt`` for the prep's parsing."""
    module = ModuleType("homeassistant.util.dt")

    def parse_datetime(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    module.parse_datetime = parse_datetime  # type: ignore[attr-defined]
    module.as_utc = lambda value: value.astimezone(UTC)  # type: ignore[attr-defined]
    module.utcnow = lambda: datetime.now(UTC)  # type: ignore[attr-defined]
    return module


def _ha_error() -> type[BaseException]:
    """The registered ``HomeAssistantError`` stub (full-suite import order)."""
    return sys.modules["homeassistant.exceptions"].HomeAssistantError


@pytest.fixture
def prep_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub the prep's collaborators and capture the processor construction."""
    monkeypatch.setattr(component_logbook, "dt_util", _dt_module())
    monkeypatch.setattr(
        component_logbook, "async_filter_entities", lambda hass, ids: ids
    )
    monkeypatch.setattr(
        component_logbook,
        "async_determine_event_types",
        lambda hass, entity_ids, device_ids: ["state_changed"],
    )
    rows = [{"when": "2026-06-16T00:00:00+00:00", "name": "Kitchen Light"}]
    processor = MagicMock()
    processor.get_events.return_value = rows
    processor_cls = MagicMock(return_value=processor)
    monkeypatch.setattr(component_logbook, "EventProcessor", processor_cls)
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value=rows)
    monkeypatch.setattr(component_logbook, "get_instance", lambda hass: recorder)
    return SimpleNamespace(
        processor_cls=processor_cls,
        processor=processor,
        recorder=recorder,
        rows=rows,
    )


def _msg(**overrides):
    msg = {
        "start_time": "2026-06-16T00:00:00+00:00",
        "end_time": "2026-06-16T12:00:00+00:00",
    }
    msg.update(overrides)
    return msg


@pytest.mark.asyncio
async def test_prep_runs_the_rest_shaped_processor_flags(prep_env) -> None:
    result = await component_logbook._logbook_events_prep(
        MagicMock(),
        _msg(entity_ids=["light.a"], device_ids=["dev1"]),
    )

    assert result == {"events": prep_env.rows}
    args, kwargs = prep_env.processor_cls.call_args
    assert kwargs["timestamp"] is False
    assert kwargs["include_entity_name"] is True
    assert args[2] == ["light.a"]
    assert args[3] == ["dev1"]
    # The recorder query runs in the executor, off the event loop.
    prep_env.recorder.async_add_executor_job.assert_awaited_once()
    assert prep_env.recorder.async_add_executor_job.await_args.args[0] is (
        prep_env.processor.get_events
    )


@pytest.mark.asyncio
async def test_invalid_start_time_raises(prep_env) -> None:
    with pytest.raises(_ha_error(), match="Invalid start_time"):
        await component_logbook._logbook_events_prep(
            MagicMock(), _msg(start_time="not-a-time")
        )
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_end_time_raises(prep_env) -> None:
    with pytest.raises(_ha_error(), match="Invalid end_time"):
        await component_logbook._logbook_events_prep(
            MagicMock(), _msg(end_time="not-a-time")
        )
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_future_start_time_returns_empty_without_querying(prep_env) -> None:
    result = await component_logbook._logbook_events_prep(
        MagicMock(), _msg(start_time="2030-01-01T00:00:00+00:00")
    )
    assert result == {"events": []}
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_filtered_away_entities_return_empty(
    prep_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        component_logbook, "async_filter_entities", lambda hass, ids: []
    )
    result = await component_logbook._logbook_events_prep(
        MagicMock(), _msg(entity_ids=["light.gone"])
    )
    assert result == {"events": []}
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_context_cannot_combine_with_other_filters(prep_env) -> None:
    with pytest.raises(_ha_error(), match="context_id cannot be combined"):
        await component_logbook._logbook_events_prep(
            MagicMock(), _msg(entity_ids=["light.a"], context_id="ctx1")
        )
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_omitted_end_time_defaults_to_now(prep_env) -> None:
    msg = _msg()
    msg.pop("end_time")
    await component_logbook._logbook_events_prep(MagicMock(), msg)

    start, end = prep_env.recorder.async_add_executor_job.await_args.args[1:]
    assert start == datetime(2026, 6, 16, tzinfo=UTC)
    assert end >= datetime.now(UTC).replace(microsecond=0)


def test_formatter_wraps_the_rows() -> None:
    rows = [{"when": "2026-06-16T00:00:00+00:00"}]
    assert component_logbook._do_logbook_events(MagicMock(), {}, events=rows) == {
        "events": rows
    }
    assert component_logbook._do_logbook_events(MagicMock(), {}) == {"events": []}
