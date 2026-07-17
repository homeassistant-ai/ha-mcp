"""Routing tests for ``ha_manage_backup`` snapshot create/restore over the
``ha_mcp_tools`` component gate.

``create_backup`` and ``restore_backup`` each resolved the local backup agent
and the default password via two sequential legacy WS calls
(``backup/agents/info`` then ``backup/config/info``). When the component
advertises ``backup_prep``, ONE ``ha_mcp_tools/backup_prep`` read supplies
both fields in a single frame — these tests pin that the sequential legacy
probes are skipped when the component serves the read, restored on every
degradation (capability miss, ``unknown_command``, non-unknown command error),
and that a ``local_agent_id: None`` component payload produces the SAME "no
local agent" failure the legacy path raises, while a missing
``default_password`` diverges exactly the way the legacy helpers already
diverge between the two call sites (``create_backup`` raises;
``restore_backup`` warns and proceeds without a safety backup).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import backup as backup_module
from ha_mcp.tools import component_api

from ._component_routing_helpers import make_ws, patch_ws

_CAPS_BACKUP_PREP = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["backup_prep"],
    "limits": {},
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.1.1",
    "capabilities": [],
    "limits": {},
}


def _client() -> MagicMock:
    client = MagicMock()
    client.base_url = "http://ha.local:8123"
    client.token = "tok"
    client.verify_ssl = False
    return client


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _legacy_ws(
    *, agent_id: str = "hassio.local", password: str | None = "pw"
) -> AsyncMock:
    """A scripted ephemeral WS client for the LEGACY sequential path.

    Raises on any command not explicitly scripted, so a stray fallback call
    (the whole point of these routing tests) fails loudly.
    """
    ws = AsyncMock()
    responses = {
        "backup/agents/info": {
            "success": True,
            "result": {"agents": [{"agent_id": agent_id, "name": "local"}]},
        },
        "backup/config/info": {
            "success": True,
            "result": {"config": {"create_backup": {"password": password}}},
        },
        "backup/generate": {
            "success": True,
            "result": {"backup_job_id": "job-1"},
        },
        "backup/info": {
            "success": True,
            "result": {"backups": [{"backup_id": "target-slug"}]},
        },
        "backup/restore": {"success": True},
    }

    async def _send(command: str, **_kwargs: Any) -> Any:
        if command not in responses:
            raise AssertionError(f"unexpected legacy WS command: {command!r}")
        return responses[command]

    ws.send_command = AsyncMock(side_effect=_send)
    ws.disconnect = AsyncMock()
    return ws


def _backup_prep_calls(ws: AsyncMock) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == backup_module.WS_BACKUP_PREP
    ]


# =============================================================================
# create_backup
# =============================================================================
class TestCreateBackupRouting:
    @pytest.mark.asyncio
    async def test_component_preferred_skips_legacy_probes(self) -> None:
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_result={
                "agent_ids": ["hassio.local"],
                "local_agent_id": "hassio.local",
                "default_password": "pw",
            },
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True, "backup_id": "b1"}),
            ),
        ):
            result = await backup_module.create_backup(client, name="n")

        assert result["success"] is True
        assert len(_backup_prep_calls(component_ws)) == 1
        # The legacy sequential probes never ran.
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" not in legacy_calls
        assert "backup/config/info" not in legacy_calls
        # The actual generate call still used the component-resolved agent.
        generate_call = next(
            c
            for c in legacy_ws.send_command.call_args_list
            if c.args[0] == "backup/generate"
        )
        assert generate_call.kwargs["agent_ids"] == ["hassio.local"]
        assert generate_call.kwargs["password"] == "pw"

    @pytest.mark.asyncio
    async def test_capability_miss_falls_back_to_legacy_probes(self) -> None:
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_NONE,
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True, "backup_id": "b1"}),
            ),
        ):
            result = await backup_module.create_backup(client, name="n")

        assert result["success"] is True
        assert not _backup_prep_calls(component_ws)
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" in legacy_calls
        assert "backup/config/info" in legacy_calls

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param({"agent_ids": ["hassio.local"]}, id="missing_local_agent_id"),
            pytest.param("not-a-dict", id="non_dict_result"),
        ],
    )
    @pytest.mark.asyncio
    async def test_malformed_component_payload_falls_back_to_legacy_probes(
        self, malformed: Any
    ) -> None:
        """A shape-drift component reply (non-dict result, or a dict missing the
        ``local_agent_id`` key) routes to the legacy sequential probes rather than
        being trusted as an authoritative "no agents" answer (backup.py:263-266)."""
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_result=malformed,
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True, "backup_id": "b1"}),
            ),
        ):
            result = await backup_module.create_backup(client, name="n")

        assert result["success"] is True
        # The component WAS asked (one frame); its malformed reply fell back.
        assert len(_backup_prep_calls(component_ws)) == 1
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" in legacy_calls
        assert "backup/config/info" in legacy_calls
        # Shape drift is not ``unknown_command``, so caps stay cached.
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_unknown_command_invalidates_caps_and_falls_back(self) -> None:
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True, "backup_id": "b1"}),
            ),
        ):
            result = await backup_module.create_backup(client, name="n")

        assert result["success"] is True
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" in legacy_calls
        assert "backup/config/info" in legacy_calls
        assert client not in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_non_unknown_command_error_falls_back_without_invalidating(
        self,
    ) -> None:
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_exc=HomeAssistantCommandTimeout("timeout"),
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True, "backup_id": "b1"}),
            ),
        ):
            result = await backup_module.create_backup(client, name="n")

        assert result["success"] is True
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" in legacy_calls
        assert "backup/config/info" in legacy_calls
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_connection_error_propagates_without_legacy_fallback(self) -> None:
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_exc=HomeAssistantConnectionError("ws down"),
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True, "backup_id": "b1"}),
            ),
            pytest.raises(ToolError),
        ):
            await backup_module.create_backup(client, name="n")

        # The connection error propagated instead of silently trying the
        # legacy probes (which share the same unreachable host).
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" not in legacy_calls

    @pytest.mark.asyncio
    async def test_component_no_local_agent_raises_same_error_as_legacy(self) -> None:
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_result={
                "agent_ids": ["remote.s3"],
                "local_agent_id": None,
                "default_password": "pw",
            },
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(_legacy_ws(), None)),
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await backup_module.create_backup(client, name="n")

        assert "No local backup agent found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_component_no_password_raises(self) -> None:
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_result={
                "agent_ids": ["hassio.local"],
                "local_agent_id": "hassio.local",
                "default_password": None,
            },
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(_legacy_ws(), None)),
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await backup_module.create_backup(client, name="n")

        assert "No default backup password configured" in str(excinfo.value)


# =============================================================================
# restore_backup
# =============================================================================
class TestRestoreBackupRouting:
    @pytest.mark.asyncio
    async def test_component_preferred_skips_legacy_probes(self) -> None:
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_result={
                "agent_ids": ["hassio.local"],
                "local_agent_id": "hassio.local",
                "default_password": "pw",
            },
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True}),
            ),
        ):
            result = await backup_module.restore_backup(client, "target-slug")

        assert result["success"] is True
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" not in legacy_calls
        assert "backup/config/info" not in legacy_calls
        assert result["safety_backup_id"] is not None

    @pytest.mark.asyncio
    async def test_capability_miss_falls_back_to_legacy_probes(self) -> None:
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_NONE,
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
            patch(
                "ha_mcp.tools.backup._poll_backup_completion",
                new=AsyncMock(return_value={"success": True}),
            ),
        ):
            result = await backup_module.restore_backup(client, "target-slug")

        assert result["success"] is True
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" in legacy_calls
        assert "backup/config/info" in legacy_calls

    @pytest.mark.asyncio
    async def test_component_no_local_agent_raises_same_error_as_legacy(self) -> None:
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_result={
                "agent_ids": ["remote.s3"],
                "local_agent_id": None,
                "default_password": "pw",
            },
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(_legacy_ws(), None)),
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await backup_module.restore_backup(client, "target-slug")

        assert "No local backup agent found" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_component_no_password_continues_without_safety_backup(
        self,
    ) -> None:
        """Restore tolerates a missing default password (unlike create) —
        it proceeds with no safety backup rather than raising, mirroring the
        legacy ``except ToolError`` catch around ``_get_backup_password``."""
        legacy_ws = _legacy_ws()
        component_ws = make_ws(
            backup_module.WS_BACKUP_PREP,
            info_result=_CAPS_BACKUP_PREP,
            cmd_result={
                "agent_ids": ["hassio.local"],
                "local_agent_id": "hassio.local",
                "default_password": None,
            },
        )
        client = _client()

        with (
            patch_ws(component_ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(legacy_ws, None)),
            ),
        ):
            result = await backup_module.restore_backup(client, "target-slug")

        assert result["success"] is True
        assert result["safety_backup_id"] is None
        # No safety backup means no generate call, and the legacy probes
        # were skipped (the component answered authoritatively).
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/generate" not in legacy_calls
        assert "backup/agents/info" not in legacy_calls
        assert "backup/config/info" not in legacy_calls
