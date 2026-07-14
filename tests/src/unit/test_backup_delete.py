"""Unit tests for the snapshot-delete path in `backup.py` (#1861).

`ha_manage_backup(scope="snapshot", action="delete")` did not exist before
this change — snapshots could be created but never removed, so an agent
that filled the disk with accumulated snapshots had no way to free space.

Adding delete widens the tool's blast radius (a full HA snapshot may be the
last recovery point after the agent itself broke something), so the
implementation is layered:

L0. `enable_snapshot_delete` (default False) — a human-only opt-in via env
    var / web-settings override file / add-on Supervisor options. An agent
    cannot flip this on itself.
L1. Scheduled backups (`with_automatic_settings=True`) are never deletable —
    they are the user's real safety net and HA's own retention already
    manages them.
L2. An age floor (`snapshot_delete_min_age_days`, default 7) — count-based
    "keep the last N" is defeatable by an agent flooding new backups before
    deleting old ones, but the HA-stamped creation date cannot be forged.
L3. The single newest remaining snapshot is never deletable, regardless of
    type — guarantees at least one recovery point always survives.
Plus a `confirm=True` requirement as a cheap backstop.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.errors import ErrorCode
from ha_mcp.tools.backup import delete_backup


def _now() -> datetime:
    """Real wall-clock time, computed fresh per call rather than frozen —
    the guard logic under test compares against `datetime.now(UTC)`
    directly, so offsets relative to a live clock are simpler and more
    robust than mocking the `datetime` module."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _entry(
    backup_id: str,
    *,
    date: str,
    with_automatic_settings: bool = False,
    name: str = "Some_Backup",
) -> dict[str, Any]:
    return {
        "backup_id": backup_id,
        "name": name,
        "date": date,
        "with_automatic_settings": with_automatic_settings,
    }


def _ws_client(
    backups: list[dict[str, Any]],
    *,
    delete_agent_errors: dict[str, str] | None = None,
) -> AsyncMock:
    ws = AsyncMock()

    async def _send(command: str, **kwargs: Any) -> Any:
        if command == "backup/info":
            return {"success": True, "result": {"backups": backups}}
        if command == "backup/delete":
            return {
                "success": True,
                "result": {"agent_errors": delete_agent_errors or {}},
            }
        raise AssertionError(f"unexpected WS command: {command!r}")

    ws.send_command.side_effect = _send
    return ws


def _settings(*, enabled: bool, min_age_days: int = 7) -> MagicMock:
    settings = MagicMock()
    settings.enable_snapshot_delete = enabled
    settings.snapshot_delete_min_age_days = min_age_days
    return settings


def _client() -> MagicMock:
    return MagicMock(base_url="http://ha", token="tok", verify_ssl=True)


def _patched(ws: AsyncMock, settings: MagicMock):
    return (
        patch(
            "ha_mcp.tools.backup.get_connected_ws_client",
            new=AsyncMock(return_value=(ws, None)),
        ),
        patch("ha_mcp.tools.backup.get_global_settings", return_value=settings),
    )


class TestSnapshotDeleteGate:
    @pytest.mark.asyncio
    async def test_disabled_by_default_raises_without_any_ws_call(self) -> None:
        ws = _ws_client([_entry("target", date=_iso(_now() - timedelta(days=30)))])
        settings = _settings(enabled=False)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "target", confirm=True)
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        ws.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_requires_confirm_true(self) -> None:
        ws = _ws_client([_entry("target", date=_iso(_now() - timedelta(days=30)))])
        settings = _settings(enabled=True)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "target", confirm=False)
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        assert "confirm" in str(exc_info.value).lower()
        ws.send_command.assert_not_awaited()


class TestSnapshotDeleteLookup:
    @pytest.mark.asyncio
    async def test_backup_not_found_raises(self) -> None:
        ws = _ws_client([_entry("other", date=_iso(_now() - timedelta(days=30)))])
        settings = _settings(enabled=True)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "missing-id", confirm=True)
        assert ErrorCode.RESOURCE_NOT_FOUND.value in str(exc_info.value)


class TestSnapshotDeleteGuards:
    @pytest.mark.asyncio
    async def test_refuses_automatic_settings_backup(self) -> None:
        """L1: scheduled backups are never deletable, even if old and not
        the newest."""
        old = _iso(_now() - timedelta(days=100))
        newer = _iso(_now() - timedelta(days=1))
        ws = _ws_client(
            [
                _entry("target", date=old, with_automatic_settings=True),
                _entry("other", date=newer),
            ]
        )
        settings = _settings(enabled=True)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "target", confirm=True)
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        assert "automatic" in str(exc_info.value).lower()
        assert not any(
            c.args[0] == "backup/delete" for c in ws.send_command.call_args_list
        )

    @pytest.mark.asyncio
    async def test_refuses_too_young_backup(self) -> None:
        """L2: a backup newer than the age floor cannot be deleted, even
        when it is not the single newest one."""
        young = _iso(_now() - timedelta(days=1))
        newest = _iso(_now())
        ws = _ws_client(
            [
                _entry("target", date=young),
                _entry("other", date=newest),
            ]
        )
        settings = _settings(enabled=True, min_age_days=7)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "target", confirm=True)
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        assert "days" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_refuses_deleting_newest_snapshot(self) -> None:
        """L3: the single newest snapshot overall is protected, even though
        it individually clears the age floor."""
        older = _iso(_now() - timedelta(days=200))
        target_date = _iso(_now() - timedelta(days=100))
        ws = _ws_client(
            [
                _entry("other", date=older),
                _entry("target", date=target_date),
            ]
        )
        settings = _settings(enabled=True, min_age_days=7)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "target", confirm=True)
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        assert "newest" in str(exc_info.value).lower()
        assert not any(
            c.args[0] == "backup/delete" for c in ws.send_command.call_args_list
        )

    @pytest.mark.asyncio
    async def test_malformed_date_fails_closed(self) -> None:
        """A missing/unparseable `date` on the target must refuse deletion
        rather than silently treat it as old-enough."""
        entry = _entry("target", date=_iso(_now() - timedelta(days=100)))
        entry["date"] = None
        ws = _ws_client([entry])
        settings = _settings(enabled=True, min_age_days=7)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "target", confirm=True)
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        assert not any(
            c.args[0] == "backup/delete" for c in ws.send_command.call_args_list
        )

    @pytest.mark.asyncio
    async def test_min_age_days_zero_disables_age_floor(self) -> None:
        """min_age_days=0 (opt-out) allows a recent backup through, as long
        as it is not the newest and not automatic."""
        recent = _iso(_now() - timedelta(hours=1))
        newest = _iso(_now())
        ws = _ws_client(
            [
                _entry("target", date=recent),
                _entry("other", date=newest),
            ]
        )
        settings = _settings(enabled=True, min_age_days=0)
        p1, p2 = _patched(ws, settings)
        with p1, p2:
            result = await delete_backup(_client(), "target", confirm=True)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_newest_guard_survives_with_age_floor_disabled(self) -> None:
        """L3 (newest-snapshot protection) must fire independently of L2 (the
        age floor) — an admin disabling the age floor must not also,
        accidentally, disable the newest-snapshot guard. Regression coverage
        for a refactor that nests the newest-check inside the age-check
        block, which would pass every other existing test."""
        older = _iso(_now() - timedelta(days=200))
        newest = _iso(_now())
        ws = _ws_client(
            [
                _entry("other", date=older),
                _entry("target", date=newest),
            ]
        )
        settings = _settings(enabled=True, min_age_days=0)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "target", confirm=True)
        assert ErrorCode.VALIDATION_INVALID_PARAMETER.value in str(exc_info.value)
        assert "newest" in str(exc_info.value).lower()
        assert not any(
            c.args[0] == "backup/delete" for c in ws.send_command.call_args_list
        )


class TestSnapshotDeleteSuccess:
    @pytest.mark.asyncio
    async def test_deletes_old_non_newest_ad_hoc_backup(self) -> None:
        old = _iso(_now() - timedelta(days=100))
        newer = _iso(_now() - timedelta(days=1))
        ws = _ws_client(
            [
                _entry("target", date=old, name="Pre_Change"),
                _entry("other", date=newer),
            ]
        )
        settings = _settings(enabled=True, min_age_days=7)
        p1, p2 = _patched(ws, settings)
        with p1, p2:
            result = await delete_backup(_client(), "target", confirm=True)
        assert result["success"] is True
        assert result["backup_id"] == "target"
        delete_call = next(
            c for c in ws.send_command.call_args_list if c.args[0] == "backup/delete"
        )
        assert delete_call.kwargs["backup_id"] == "target"

    @pytest.mark.asyncio
    async def test_agent_errors_raises_service_call_failed(self) -> None:
        old = _iso(_now() - timedelta(days=100))
        newer = _iso(_now() - timedelta(days=1))
        ws = _ws_client(
            [
                _entry("target", date=old),
                _entry("other", date=newer),
            ],
            delete_agent_errors={"hassio.local": "boom"},
        )
        settings = _settings(enabled=True, min_age_days=7)
        p1, p2 = _patched(ws, settings)
        with p1, p2, pytest.raises(ToolError) as exc_info:
            await delete_backup(_client(), "target", confirm=True)
        assert ErrorCode.SERVICE_CALL_FAILED.value in str(exc_info.value)
