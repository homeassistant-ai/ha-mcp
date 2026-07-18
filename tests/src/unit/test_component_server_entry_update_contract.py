"""Unit + contract tests for the ``server_entry_update`` WRITE capability.

The write counterpart of ``server_entry`` (issue #1813 Phase 3): the component
applies a ``channel`` / ``pip_spec`` delta to its OWN server config entry via
``hass.config_entries.async_update_entry`` DIRECTLY, DEFERRED on a hass-level
background task so the WS response flushes before the resulting self-reload tears
the serving thread down. These pin the load-bearing behaviours:

* the merged options preserve every existing key (URL/secret overrides included);
* ``async_update_entry`` is NOT called synchronously — the prep returns first, and
  only the deferred task applies it (the deferred-reload crux);
* a no-op (merged == current) returns ``unchanged`` and schedules nothing;
* a missing server entry / an empty delta raises ``HomeAssistantError`` (→ the
  server's command-error fallback to its legacy options-flow submit).

Imports the Fake* fixtures + the stub-installing ``wsapi`` handle through
``test_component_ws_search`` (mirrors ``test_component_ws_phase2_async``).
"""

from __future__ import annotations

from typing import Any

import pytest

from . import test_component_ws_search as _base
from .test_component_ws_search import FakeConfigEntry, wsapi

_REAL_VOL = _base._REAL_VOL


class _RecordingConfigEntries:
    """``hass.config_entries`` fake: enumerates entries + records update writes."""

    def __init__(self, entries: list[Any]) -> None:
        self._entries = list(entries)
        self.update_calls: list[tuple[Any, dict[str, Any] | None]] = []

    def async_entries(self) -> list[Any]:
        return list(self._entries)

    def async_update_entry(
        self, entry: Any, *, options: Any = None, **_kw: Any
    ) -> bool:
        self.update_calls.append(
            (entry, dict(options) if options is not None else None)
        )
        if options is not None:
            entry.options = dict(options)
        return True


class _BgHass:
    """Minimal hass whose ``async_create_background_task`` CAPTURES the coroutine.

    Capturing (rather than scheduling) lets a test assert the deferred
    ``async_update_entry`` has NOT run right after the prep returns, then drive it
    explicitly by awaiting the captured coroutine — proving the deferral.
    """

    def __init__(self, entries: list[Any]) -> None:
        self.config_entries = _RecordingConfigEntries(entries)
        self.data: dict[str, Any] = {}
        self.scheduled: list[Any] = []

    def async_create_background_task(self, coro: Any, name: str | None = None) -> Any:
        self.scheduled.append(coro)
        return coro


def _server_entry(
    *, options: dict[str, Any] | None = None, entry_id: str = "srv1"
) -> FakeConfigEntry:
    return FakeConfigEntry(
        domain="ha_mcp_tools",
        data={"entry_type": "server", "webhook_id": "secret-xyz"},
        options=options or {},
        entry_id=entry_id,
    )


@pytest.fixture(autouse=True)
def _fast_flush(monkeypatch: Any) -> None:
    """Collapse the flush delay so awaiting the deferred task is instant."""
    monkeypatch.setattr(wsapi, "SERVER_ENTRY_UPDATE_FLUSH_DELAY_S", 0)


@pytest.mark.asyncio
async def test_prep_schedules_merged_update_preserving_other_keys() -> None:
    """A channel delta schedules async_update_entry with the MERGED options (the
    server_url override and current pip_spec preserved), returns scheduled:True,
    and does NOT call async_update_entry synchronously."""
    entry = _server_entry(
        options={
            "channel": "stable",
            "pip_spec": "",
            "server_url": "http://ha.local:8123",
        }
    )
    hass = _BgHass([entry])

    extra = await wsapi._server_entry_update_prep(
        hass, {"type": wsapi.WS_SERVER_ENTRY_UPDATE, "channel": "dev"}
    )
    result = wsapi._do_server_entry_update(
        hass, {"type": wsapi.WS_SERVER_ENTRY_UPDATE, "channel": "dev"}, **extra
    )

    # Pure formatter returns exactly the prep's envelope.
    assert result is extra["result"]
    assert result["scheduled"] is True
    assert result["entry_id"] == "srv1"
    assert result["applying"] == {"channel": "dev"}
    assert result["previous"] == {"channel": "stable", "pip_spec": ""}
    # Deferred-reload crux: NOT applied synchronously.
    assert hass.config_entries.update_calls == []
    assert len(hass.scheduled) == 1

    # Drive the deferred task: now the merged options are applied, other keys kept.
    await hass.scheduled[0]
    assert len(hass.config_entries.update_calls) == 1
    _applied_entry, applied_options = hass.config_entries.update_calls[0]
    assert applied_options == {
        "channel": "dev",
        "pip_spec": "",
        "server_url": "http://ha.local:8123",
    }


@pytest.mark.asyncio
async def test_prep_pip_spec_applied_preserves_channel() -> None:
    """A pip_spec delta preserves the current channel in the merged options."""
    entry = _server_entry(options={"channel": "dev", "pip_spec": "ha-mcp==1.0.0"})
    hass = _BgHass([entry])

    extra = await wsapi._server_entry_update_prep(
        hass,
        {"type": wsapi.WS_SERVER_ENTRY_UPDATE, "pip_spec": "ha-mcp==2.0.0"},
    )
    assert extra["result"]["applying"] == {"pip_spec": "ha-mcp==2.0.0"}
    await hass.scheduled[0]
    _entry, applied = hass.config_entries.update_calls[0]
    assert applied == {"channel": "dev", "pip_spec": "ha-mcp==2.0.0"}


@pytest.mark.asyncio
async def test_prep_noop_returns_unchanged_without_scheduling() -> None:
    """Setting channel to its current value is a no-op: unchanged, nothing
    scheduled, async_update_entry never called."""
    entry = _server_entry(options={"channel": "dev", "pip_spec": ""})
    hass = _BgHass([entry])

    extra = await wsapi._server_entry_update_prep(
        hass, {"type": wsapi.WS_SERVER_ENTRY_UPDATE, "channel": "dev"}
    )
    result = extra["result"]
    assert result["scheduled"] is False
    assert result["unchanged"] is True
    assert result["previous"] == {"channel": "dev", "pip_spec": ""}
    assert hass.scheduled == []
    assert hass.config_entries.update_calls == []


@pytest.mark.asyncio
async def test_prep_no_server_entry_raises() -> None:
    """No server config entry → HomeAssistantError (server maps it to a
    command-error fallback)."""
    tools = FakeConfigEntry(
        domain="ha_mcp_tools", data={"entry_type": "tools"}, entry_id="tools1"
    )
    hass = _BgHass([tools])
    with pytest.raises(_base._StubHomeAssistantError):
        await wsapi._server_entry_update_prep(
            hass, {"type": wsapi.WS_SERVER_ENTRY_UPDATE, "channel": "dev"}
        )
    assert hass.scheduled == []


@pytest.mark.asyncio
async def test_prep_requires_at_least_one_field() -> None:
    """A frame with neither channel nor pip_spec raises (defence-in-depth over the
    server, which always sends at least one)."""
    hass = _BgHass([_server_entry(options={"channel": "stable"})])
    with pytest.raises(_base._StubHomeAssistantError):
        await wsapi._server_entry_update_prep(
            hass, {"type": wsapi.WS_SERVER_ENTRY_UPDATE}
        )
    assert hass.scheduled == []


@pytest.mark.asyncio
async def test_prep_clearing_pip_spec_from_absent_schedules() -> None:
    """An empty-string pip_spec on an entry that never had the key is a structural
    change (clear the override) — it schedules rather than short-circuiting."""
    entry = _server_entry(options={"channel": "dev"})
    hass = _BgHass([entry])
    extra = await wsapi._server_entry_update_prep(
        hass, {"type": wsapi.WS_SERVER_ENTRY_UPDATE, "pip_spec": ""}
    )
    assert extra["result"]["scheduled"] is True
    await hass.scheduled[0]
    _entry, applied = hass.config_entries.update_calls[0]
    assert applied == {"channel": "dev", "pip_spec": ""}


# =============================================================================
# schema (real voluptuous)
# =============================================================================
class TestServerEntryUpdateSchema:
    def test_accepts_channel_and_pip_spec(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._server_entry_update_schema())
        out = schema(
            {
                "type": wsapi.WS_SERVER_ENTRY_UPDATE,
                "channel": "dev",
                "pip_spec": "ha-mcp==1.0.0",
            }
        )
        assert out["channel"] == "dev"
        assert out["pip_spec"] == "ha-mcp==1.0.0"

    def test_type_only_validates_prep_enforces_at_least_one(
        self, monkeypatch: Any
    ) -> None:
        # The schema alone permits neither field (voluptuous cannot express
        # "at least one of"); the prep is what rejects an empty delta.
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._server_entry_update_schema())
        out = schema({"type": wsapi.WS_SERVER_ENTRY_UPDATE})
        assert out == {"type": wsapi.WS_SERVER_ENTRY_UPDATE}

    def test_rejects_multiline_pip_spec(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._server_entry_update_schema())
        with pytest.raises(_REAL_VOL.Invalid):
            schema({"type": wsapi.WS_SERVER_ENTRY_UPDATE, "pip_spec": "a\nb"})

    def test_rejects_overlong_pip_spec(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._server_entry_update_schema())
        with pytest.raises(_REAL_VOL.Invalid):
            schema(
                {
                    "type": wsapi.WS_SERVER_ENTRY_UPDATE,
                    "pip_spec": "x" * (wsapi.SERVER_ENTRY_UPDATE_MAX_PIP_SPEC + 1),
                }
            )

    def test_rejects_extra_keys(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._server_entry_update_schema())
        with pytest.raises(_REAL_VOL.Invalid):
            schema({"type": wsapi.WS_SERVER_ENTRY_UPDATE, "bogus": "x"})
