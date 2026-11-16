"""Integration auto-backups: the snapshot carries the entry's options when the
component can read them safely, and a restore puts them back through the
options flow before it reapplies the enabled state. A deleted entry is refused.

The tests where the component has no answer run the server's client on a real
``hass``: Core holds the entries and answers every command, with or without
the component's own commands."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, MockModule, mock_integration

from custom_components.ha_mcp_tools.websocket_api.constants import WS_CONFIG_ENTRIES
from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_integrations import fetch_integration, restore_integration
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.redaction import REDACTED_SET
from ha_mcp.tools import backup as backup_tool
from ha_mcp.tools import component_config_entries, config_entry_flow
from ha_mcp.tools.component_config_entries import ComponentEntries

from ._component_frames import dropped, pooled

_DOMAIN = "backup_planes"


def _ws(monkeypatch: pytest.MonkeyPatch, component: list[ComponentEntries]) -> list:
    """Record what reaches Core; ``component`` answers successive reads (last repeats)."""
    sent: list[dict[str, Any]] = []
    reads = list(component)

    async def fake_ws_send(client: Any, message: dict[str, Any]) -> Any:
        sent.append(message)
        return {"require_restart": False}

    async def fake_component_read(client: Any, *, entry_id: str) -> Any:
        sent.append({"type": "ha_mcp_tools/config_entries", "entry_id": entry_id})
        return reads.pop(0) if len(reads) > 1 else reads[0]

    monkeypatch.setattr(bm, "_ws_send", fake_ws_send)
    monkeypatch.setattr(
        component_config_entries, "fetch_config_entries", fake_component_read
    )
    return sent


@pytest.fixture
async def planes(hass: HomeAssistant) -> None:
    """An integration whose entries load and unload, and Core's entry commands."""

    async def loaded(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        return True

    mock_integration(
        hass, MockModule(_DOMAIN, async_setup_entry=loaded, async_unload_entry=loaded)
    )
    assert await async_setup_component(hass, "config", {})


def _entry(
    hass: HomeAssistant, disabled_by: ConfigEntryDisabler | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=_DOMAIN, options={"int": 11}, disabled_by=disabled_by
    )
    entry.add_to_hass(hass)
    return entry


def _read(*rows: dict[str, Any], degraded: bool = False) -> ComponentEntries:
    return ComponentEntries(rows=list(rows), secret_scrub_degraded=degraded)


def _row(options: dict[str, Any], disabled_by: str | None = None) -> dict[str, Any]:
    return {
        "entry_id": "e1",
        "domain": "demo",
        "disabled_by": disabled_by,
        "unique_id": "u1",
        "options": options,
        "subentries": [{"subentry_id": "s1"}],
    }


async def test_restoring_a_deleted_entry_is_refused_and_changes_nothing(
    hass: HomeAssistant, core_client: HomeAssistantClient, planes: None
) -> None:
    other = _entry(hass, ConfigEntryDisabler.USER)
    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await restore_integration(
            core_client, "e1", {"domain": "otp", "disabled_by": None}
        )
    assert exc_info.value.outcome["reason"] == "entry_deleted"
    assert exc_info.value.outcome["apply_status"] == "not_applied"
    assert "Config entry e1 (otp) no longer exists" in str(exc_info.value)
    assert hass.config_entries.async_entries() == [other]
    assert other.disabled_by is ConfigEntryDisabler.USER


@pytest.mark.parametrize(
    ("snapshot", "current", "expected"),
    [
        ("user", None, ConfigEntryDisabler.USER),
        (None, ConfigEntryDisabler.USER, None),
    ],
    ids=["disables", "enables"],
)
async def test_a_snapshot_taken_before_options_were_kept_reports_them_not_restored(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    planes: None,
    snapshot: str | None,
    current: ConfigEntryDisabler | None,
    expected: ConfigEntryDisabler | None,
) -> None:
    """The enabled state is still put back; the options are reported."""
    entry = _entry(hass, current)
    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await restore_integration(
            core_client, entry.entry_id, {"domain": _DOMAIN, "disabled_by": snapshot}
        )
    assert exc_info.value.outcome["reason"] == "options_not_restored"
    assert exc_info.value.outcome["apply_status"] == "applied"
    assert exc_info.value.outcome["verification_status"] == "unavailable"
    assert "taken before options were captured" in str(exc_info.value)
    assert entry.disabled_by is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safety,expected",
    [
        (None, ["Add it again"]),
        ("safety.yaml", ["Add it again", "Use safety_backup to inspect or restore"]),
    ],
)
async def test_a_refused_restore_is_not_found_with_the_handlers_guidance(
    safety: str | None, expected: list[str]
) -> None:
    outcome: dict[str, Any] = {"suggestions": ["Add it again"]}
    if safety:
        outcome["safety_backup"] = safety
    mgr = MagicMock()
    mgr.restore_snapshot = AsyncMock(
        side_effect=bm.BackupRestoreError("gone", reason="entry_deleted", **outcome)
    )
    with pytest.raises(ToolError) as exc_info:
        await backup_tool._edits_restore(mgr, "edits", "restore", "b.yaml")
    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "RESOURCE_NOT_FOUND"
    suggestions = error.get("suggestions") or [error["suggestion"]]
    for suggestion, prefix in zip(suggestions, expected, strict=True):
        assert suggestion.startswith(prefix)


@pytest.mark.asyncio
async def test_a_refused_restore_without_handler_guidance_gets_the_generic_hint() -> (
    None
):
    mgr = MagicMock()
    mgr.restore_snapshot = AsyncMock(
        side_effect=bm.BackupRestoreError("refused", reason="restore_refused")
    )
    with pytest.raises(ToolError) as exc_info:
        await backup_tool._edits_restore(mgr, "edits", "restore", "b.yaml")
    error = json.loads(str(exc_info.value))["error"]
    suggestions = error.get("suggestions") or [error["suggestion"]]
    assert suggestions == [
        "Inspect the current configuration and restore outcome before retrying"
    ]


# --- capture ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_through_the_component_carries_options_but_not_subentries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ws(monkeypatch, [_read(_row({"int": 11}))])

    snapshot = await fetch_integration(None, "e1")

    assert snapshot["options"] == {"int": 11}
    assert snapshot["unique_id"] == "u1"
    assert "subentries" not in snapshot
    assert "options_unavailable" not in snapshot


async def test_capture_without_the_component_keeps_cores_row_and_says_why_no_options(
    hass: HomeAssistant, core_client: HomeAssistantClient, planes: None
) -> None:
    _entry(hass)
    entry = _entry(hass)

    snapshot = await fetch_integration(core_client, entry.entry_id)

    assert snapshot["entry_id"] == entry.entry_id
    assert snapshot["domain"] == _DOMAIN
    assert snapshot["options_unavailable"] == "component_unavailable"
    assert "options" not in snapshot


async def test_capture_whose_component_read_got_no_answer_says_the_read_failed(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, planes: None
) -> None:
    """An installed component that did not answer is not reported missing."""
    _entry(hass)
    entry = _entry(hass)
    socket = await pooled(component_core_client)

    with dropped(hass, WS_CONFIG_ENTRIES, socket):
        snapshot = await fetch_integration(component_core_client, entry.entry_id)

    assert snapshot["entry_id"] == entry.entry_id
    assert snapshot["options_unavailable"] == "component_read_failed"
    assert "options" not in snapshot


@pytest.mark.asyncio
async def test_capture_of_a_missing_entry_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ws(monkeypatch, [_read()])

    assert await fetch_integration(None, "e1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "reason"),
    [
        (_read(_row({"int": 11}), degraded=True), "secret_scrub_degraded"),
        (_read(_row({"token": "**redacted**"})), "redacted_options"),
        (_read(_row({"nested": {"key": REDACTED_SET}})), "redacted_options"),
        (
            _read({k: v for k, v in _row({}).items() if k != "options"}),
            "options_missing",
        ),
    ],
)
async def test_options_that_cannot_be_captured_are_left_out_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, read: ComponentEntries, reason: str
) -> None:
    _ws(monkeypatch, [read])

    snapshot = await fetch_integration(None, "e1")

    assert "options" not in snapshot
    assert snapshot["options_unavailable"] == reason


# --- restore ------------------------------------------------------------------


async def _restore(disabled_by: str | None = None, **snapshot: Any) -> None:
    await restore_integration(
        None, "e1", {"domain": "demo", "disabled_by": disabled_by, **snapshot}
    )


def _options_flow(monkeypatch: pytest.MonkeyPatch, sent: list) -> AsyncMock:
    async def apply(client: Any, entry_id: str, options: dict[str, Any], **kw: Any):
        sent.append({"type": "options_flow", "entry_id": entry_id, "options": options})
        return {"success": True}

    update = AsyncMock(side_effect=apply)
    monkeypatch.setattr(config_entry_flow, "restore_config_entry_options", update)
    return update


@pytest.mark.asyncio
async def test_restore_puts_back_differing_options_then_the_enabled_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _ws(monkeypatch, [_read(_row({"int": 42})), _read(_row({"int": 11}))])
    update = _options_flow(monkeypatch, sent)

    await _restore(options={"int": 11})

    update.assert_awaited_once()
    assert update.await_args.kwargs == {
        "expected_domain": "demo",
        "fixed_options": {"int": 42},
    }
    assert [m["type"] for m in sent] == [
        "ha_mcp_tools/config_entries",
        "options_flow",
        "ha_mcp_tools/config_entries",
        "config_entries/disable",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"int": 11}, {}], ids=["non-empty", "empty"])
async def test_restore_runs_no_options_flow_when_the_options_already_match(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    sent = _ws(monkeypatch, [_read(_row(options))])
    update = _options_flow(monkeypatch, sent)

    await _restore(options=options)

    update.assert_not_called()
    assert sent[-1]["type"] == "config_entries/disable"


@pytest.mark.asyncio
async def test_restore_enables_a_disabled_entry_before_its_options_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _ws(
        monkeypatch,
        [_read(_row({"int": 42}, disabled_by="user")), _read(_row({"int": 11}))],
    )
    _options_flow(monkeypatch, sent)

    await _restore(options={"int": 11})

    types = [m["type"] for m in sent]
    assert types.index("config_entries/disable") < types.index("options_flow")
    enable = next(m for m in sent if m["type"] == "config_entries/disable")
    assert enable["disabled_by"] is None
    assert types.count("config_entries/disable") == 1


@pytest.mark.asyncio
async def test_restore_disables_only_after_the_options_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _ws(monkeypatch, [_read(_row({"int": 42})), _read(_row({"int": 11}))])
    _options_flow(monkeypatch, sent)

    await _restore(disabled_by="user", options={"int": 11})

    assert [m["type"] for m in sent][-2:] == [
        "ha_mcp_tools/config_entries",
        "config_entries/disable",
    ]
    assert sent[-1]["disabled_by"] == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reads", "verification"),
    [
        ([_read(_row({"int": 42}))], "mismatched"),
        (
            [_read(_row({"int": 42})), _read(_row({"int": 11}), degraded=True)],
            "unavailable",
        ),
    ],
    ids=["do-not-read-back", "readback-no-longer-scrubbed"],
)
async def test_restore_whose_options_cannot_be_verified_reports_it(
    monkeypatch: pytest.MonkeyPatch,
    reads: list[ComponentEntries | None],
    verification: str,
) -> None:
    sent = _ws(monkeypatch, reads)
    _options_flow(monkeypatch, sent)

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(options={"int": 11})

    assert exc_info.value.outcome["apply_status"] == "applied"
    assert exc_info.value.outcome["verification_status"] == verification


async def test_snapshot_options_are_not_restored_over_options_it_cannot_read(
    hass: HomeAssistant, core_client: HomeAssistantClient, planes: None
) -> None:
    entry = _entry(hass)

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await restore_integration(
            core_client,
            entry.entry_id,
            {"domain": _DOMAIN, "disabled_by": "user", "options": {"int": 42}},
        )

    assert exc_info.value.outcome["reason"] == "options_unreadable"
    assert entry.options == {"int": 11}
    assert entry.disabled_by is None
    assert not hass.config_entries.options.async_progress()


@pytest.mark.asyncio
async def test_restoring_a_deleted_entry_read_through_the_component_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _ws(monkeypatch, [_read()])

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(options={"int": 1})

    assert exc_info.value.outcome["reason"] == "entry_deleted"
    assert [m["type"] for m in sent] == ["ha_mcp_tools/config_entries"]


@pytest.mark.asyncio
async def test_restore_holds_the_entry_write_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    manager = bm.BackupManager(
        SimpleNamespace(auto_backup_dir=str(tmp_path), enable_auto_backup=True),
        object(),
    )
    held: list[bool] = []

    async def read(name: str) -> dict[str, Any]:
        return {"domain": "integration", "entity_id": "e1", "config": {}}

    async def restore(name: str, data: Any, *, take_safety_backup: bool) -> Any:
        import asyncio

        held.append(manager._entry_write_owners.get("e1") is asyncio.current_task())
        return {}

    monkeypatch.setattr(manager, "_read_restore_snapshot", read)
    monkeypatch.setattr(manager, "_restore_snapshot_data", restore)

    await manager.restore_snapshot("integration_e1_x")

    assert held == [True]


# --- options a restore cannot put back are reported, not passed as success ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "disabled_by", "verification", "message"),
    [
        ({"options": {}}, "user", "mismatched", "options were not"),
        (
            {"options_unavailable": "redacted_options"},
            None,
            "unavailable",
            "redacted_options",
        ),
    ],
    ids=["empty-options", "unreadable-options"],
)
async def test_snapshot_options_that_cannot_be_put_back_are_reported_not_restored(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: dict[str, Any],
    disabled_by: str | None,
    verification: str,
    message: str,
) -> None:
    sent = _ws(monkeypatch, [_read(_row({"int": 42}))])
    update = _options_flow(monkeypatch, sent)

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(disabled_by, **snapshot)

    update.assert_not_called()
    assert exc_info.value.outcome["verification_status"] == verification
    assert exc_info.value.outcome["apply_status"] == "applied"
    assert message in str(exc_info.value)
    assert sent[-1] == {
        "type": "config_entries/disable",
        "entry_id": "e1",
        "disabled_by": disabled_by,
    }


def _flow_error(
    status: str, reason: str | None = None
) -> config_entry_flow.OptionsFlowError:
    return config_entry_flow.OptionsFlowError(
        "Home Assistant did not complete the options restore",
        apply_status=status,
        entry_id="e1",
        reason=reason,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_disabled", "error", "apply_status", "reason", "prefix"),
    [
        (
            "user",
            _flow_error("not_applied", "flow_aborted"),
            "applied",
            "flow_aborted",
            "Config entry e1 was enabled, but",
        ),
        (
            "user",
            _flow_error("unknown"),
            "applied",
            None,
            "Config entry e1 was enabled, then its",
        ),
        (
            None,
            _flow_error("not_applied", "flow_aborted"),
            "not_applied",
            "flow_aborted",
            "",
        ),
    ],
    ids=["refused", "unsettled", "enabled-entry-keeps-own-error"],
)
async def test_a_failed_options_flow_says_whether_the_entry_was_enabled_first(
    monkeypatch: pytest.MonkeyPatch,
    row_disabled: str | None,
    error: config_entry_flow.OptionsFlowError,
    apply_status: str,
    reason: str | None,
    prefix: str,
) -> None:
    sent = _ws(monkeypatch, [_read(_row({"int": 42}, disabled_by=row_disabled))])
    monkeypatch.setattr(
        config_entry_flow, "restore_config_entry_options", AsyncMock(side_effect=error)
    )

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(options={"int": 11})

    outcome, enabled = exc_info.value.outcome, row_disabled is not None
    assert outcome["apply_status"] == apply_status
    assert outcome.get("options_apply_status") == (
        error.apply_status if enabled else None
    )
    assert outcome.get("reason") == reason
    assert str(exc_info.value).startswith(prefix)
    assert ("was enabled" in str(exc_info.value)) is enabled
    assert [m["type"] for m in sent].count("config_entries/disable") == int(enabled)


@pytest.mark.asyncio
async def test_an_integration_restore_takes_a_safety_backup_whatever_the_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _ws(monkeypatch, [_read(_row({"int": 11}))])
    manager = bm.get_backup_manager(
        SimpleNamespace(),
        SimpleNamespace(
            enable_auto_backup=False,
            auto_backup_throttle_minutes=60,
            auto_backup_retain_per_entity=5,
            auto_backup_dir=str(tmp_path),
        ),
    )
    snapshot = manager._write_snapshot(
        "integration",
        "e1",
        {"domain": "demo", "disabled_by": None, "options": {"int": 11}},
        "test",
    )

    result = await manager.restore_snapshot(snapshot.name, take_safety_backup=False)

    assert result["safety_backup"] is not None
    assert manager.read_snapshot(result["safety_backup"])["domain"] == "integration"


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [["x"], None], ids=["list", "null"])
async def test_snapshot_options_that_are_not_an_object_are_refused_before_any_write(
    monkeypatch: pytest.MonkeyPatch, options: Any
) -> None:
    sent = _ws(monkeypatch, [_read(_row({"int": 42}, disabled_by="user"))])
    update = _options_flow(monkeypatch, sent)

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(options=options)

    assert exc_info.value.outcome["reason"] == "invalid_snapshot"
    assert exc_info.value.outcome["apply_status"] == "not_applied"
    update.assert_not_called()
    assert "config_entries/disable" not in [m["type"] for m in sent]


@pytest.mark.asyncio
async def test_options_that_differ_only_in_type_are_put_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # False == 0 in Python, but the options flow stores what it is given.
    sent = _ws(monkeypatch, [_read(_row({"flag": 0})), _read(_row({"flag": False}))])
    update = _options_flow(monkeypatch, sent)

    await _restore(options={"flag": False})

    update.assert_awaited_once()
    (options,) = [m["options"] for m in sent if m["type"] == "options_flow"]
    assert options == {"flag": False}
    assert type(options["flag"]) is bool
