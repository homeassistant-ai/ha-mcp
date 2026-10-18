"""Unit tests for the component-side ``ha_mcp_tools/logbook_events`` command.

The command runs Core's own ``EventProcessor`` (with the REST flags: ISO
``when`` values, ``include_entity_name=True``) off the event loop, encodes the
result message in the same executor job (Core's own approach) and mirrors
Core's ``logbook/get_events`` validation. These tests pin the parts the
component owns against a stubbed HA: validation, the filter combination rules,
the processor flags, the executor hand-off, and the pre-serialized result seam.
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
# the sibling component test modules install); ``conftest`` supplies the
# logbook/recorder/websocket_api/json chain at module import.
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
    sys.modules.setdefault(_mod, MagicMock())

_exceptions = MagicMock()
_exceptions.HomeAssistantError = _StubHomeAssistantError
sys.modules.setdefault("homeassistant.exceptions", _exceptions)

import custom_components.ha_mcp_tools.websocket_api as ws_pkg  # noqa: E402
from custom_components.ha_mcp_tools.websocket_api import (  # noqa: E402
    logbook as component_logbook,
)
from custom_components.ha_mcp_tools.websocket_api.seam import (  # noqa: E402
    PreSerializedResult,
)

from ._component_ws_api import wsapi  # noqa: E402  (installs stubs, loads surface)
from ._component_ws_fakes import _FakeConnection, _FakeWSApi  # noqa: E402

FIXED_NOW = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


def _ha_error() -> type[BaseException]:
    """The registered ``HomeAssistantError`` stub (full-suite import order)."""
    return sys.modules["homeassistant.exceptions"].HomeAssistantError


def _dt_module(now: datetime) -> ModuleType:
    """A minimal real ``homeassistant.util.dt`` with ``utcnow`` frozen to ``now``."""
    module = ModuleType("homeassistant.util.dt")

    def parse_datetime(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    module.parse_datetime = parse_datetime  # type: ignore[attr-defined]
    module.as_utc = lambda value: value.astimezone(UTC)  # type: ignore[attr-defined]
    module.utcnow = lambda: now  # type: ignore[attr-defined]
    return module


@pytest.fixture
def prep_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub the prep's collaborators; the clock is frozen at ``FIXED_NOW``."""
    monkeypatch.setattr(component_logbook, "dt_util", _dt_module(FIXED_NOW))
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
    serializer = MagicMock(return_value=b"SERIALIZED")
    monkeypatch.setattr(component_logbook, "_serialize_logbook_result", serializer)
    empty = MagicMock(return_value=b"EMPTY")
    monkeypatch.setattr(component_logbook, "_empty_result", empty)
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value=b"SERIALIZED")
    monkeypatch.setattr(component_logbook, "get_instance", lambda hass: recorder)
    return SimpleNamespace(
        processor_cls=processor_cls,
        processor=processor,
        recorder=recorder,
        rows=rows,
        serializer=serializer,
        empty=empty,
    )


def _msg(**overrides: object) -> dict[str, object]:
    msg: dict[str, object] = {
        "id": 1,
        "start_time": "2026-06-16T00:00:00+00:00",
        "end_time": "2026-06-16T12:00:00+00:00",
    }
    msg.update(overrides)
    return msg


@pytest.mark.asyncio
async def test_prep_runs_the_rest_shaped_processor_flags(
    prep_env: SimpleNamespace,
) -> None:
    result = await component_logbook._logbook_events_prep(
        MagicMock(),
        _msg(entity_ids=["light.a"], device_ids=["dev1"]),
    )

    assert result == {"payload": b"SERIALIZED"}
    args, kwargs = prep_env.processor_cls.call_args
    assert kwargs["timestamp"] is False
    assert kwargs["include_entity_name"] is True
    assert args[2] == ["light.a"]
    assert args[3] == ["dev1"]
    # The recorder query and the encoding run in one executor job, off the
    # event loop (Core's own approach).
    prep_env.recorder.async_add_executor_job.assert_awaited_once()
    job_args = prep_env.recorder.async_add_executor_job.await_args.args
    assert job_args[0] is prep_env.serializer
    assert job_args[1] == 1
    assert job_args[2] == datetime(2026, 6, 16, tzinfo=UTC)
    assert job_args[3] == FIXED_NOW
    assert job_args[4] is prep_env.processor


def test_serializer_runs_the_query_and_encodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = MagicMock()
    messages.result_message.return_value = {"shape": "result"}
    monkeypatch.setattr(component_logbook, "messages", messages)
    encoded = MagicMock(return_value=b"BYTES")
    monkeypatch.setattr(component_logbook, "json_bytes", encoded)
    processor = MagicMock()
    processor.get_events.return_value = [{"when": "x"}]

    payload = component_logbook._serialize_logbook_result(
        7, FIXED_NOW, FIXED_NOW, processor
    )

    assert payload == b"BYTES"
    processor.get_events.assert_called_once_with(FIXED_NOW, FIXED_NOW)
    messages.result_message.assert_called_once_with(7, [{"when": "x"}])
    encoded.assert_called_once_with({"shape": "result"})


@pytest.mark.asyncio
async def test_invalid_start_time_raises(prep_env: SimpleNamespace) -> None:
    with pytest.raises(_ha_error(), match="Invalid start_time"):
        await component_logbook._logbook_events_prep(
            MagicMock(), _msg(start_time="not-a-time")
        )
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_end_time_raises(prep_env: SimpleNamespace) -> None:
    with pytest.raises(_ha_error(), match="Invalid end_time"):
        await component_logbook._logbook_events_prep(
            MagicMock(), _msg(end_time="not-a-time")
        )
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_future_start_time_returns_empty_without_querying(
    prep_env: SimpleNamespace,
) -> None:
    result = await component_logbook._logbook_events_prep(
        MagicMock(), _msg(start_time="2026-06-17T00:00:00+00:00")
    )
    assert result == {"payload": b"EMPTY"}
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_filtered_away_entities_return_empty(
    prep_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        component_logbook, "async_filter_entities", lambda hass, ids: []
    )
    result = await component_logbook._logbook_events_prep(
        MagicMock(), _msg(entity_ids=["light.gone"])
    )
    assert result == {"payload": b"EMPTY"}
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_context_cannot_combine_with_other_filters(
    prep_env: SimpleNamespace,
) -> None:
    with pytest.raises(_ha_error(), match="context_id cannot be combined"):
        await component_logbook._logbook_events_prep(
            MagicMock(), _msg(entity_ids=["light.a"], context_id="ctx1")
        )
    prep_env.processor_cls.assert_not_called()


@pytest.mark.asyncio
async def test_omitted_end_time_defaults_to_now(prep_env: SimpleNamespace) -> None:
    msg = _msg()
    del msg["end_time"]

    await component_logbook._logbook_events_prep(MagicMock(), msg)

    job_args = prep_env.recorder.async_add_executor_job.await_args.args
    assert job_args[3] == FIXED_NOW


def test_pre_serialized_result_is_sent_as_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ws = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake_ws)
    payload = b'{"id": 1, "type": "result", "success": true, "result": []}'
    handler = ws_pkg._build_handler(
        {"type": "ha_mcp_tools/test"},
        lambda hass, msg, **extra: PreSerializedResult(payload),
    )
    fake_ws.async_register_command(MagicMock(), handler)
    connection = _FakeConnection()

    handler(MagicMock(), connection, {"id": 1})

    assert connection.messages == [payload]
    assert connection.results == {}


def test_plain_result_still_uses_send_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ws = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake_ws)
    handler = ws_pkg._build_handler(
        {"type": "ha_mcp_tools/test"},
        lambda hass, msg, **extra: {"ok": True},
    )
    fake_ws.async_register_command(MagicMock(), handler)
    connection = _FakeConnection()

    handler(MagicMock(), connection, {"id": 1})

    assert connection.results == {1: {"ok": True}}
    assert connection.messages == []
