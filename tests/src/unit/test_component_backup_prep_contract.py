"""Cross-seam contract test for ``ha_mcp_tools/backup_prep``.

Wires the REAL component ``_do_backup_prep`` (driven against the
``FakeHass`` / ``_fake_backup_manager`` fixtures from
``test_component_ws_search.py``) underneath a mocked WS transport, then drives
it through the REAL ``create_backup`` flow in ``ha_mcp.tools.backup`` — so a
vocabulary or shape drift between the component's payload and the consumer's
field reads (``agent_ids`` / ``local_agent_id`` / ``default_password``) fails
here rather than shipping silently. Only the ``backup_prep`` READ is real; the
``backup/generate`` call and the completion poll (the WRITE half of
``ha_manage_backup``) are mocked — those are out of scope for this seam.

A manager-absent (or import-failure) component raises ``HomeAssistantError``
over the real transport, which the real ``HomeAssistantWebSocketClient``
reports to callers as ``HomeAssistantCommandError`` — the harness below
mirrors that translation so the fallback path is exercised the same way it
would be against a real HA instance.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import backup as backup_module
from ha_mcp.tools import component_api

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import (
    FakeHass,
    _fake_backup_agent,
    _fake_backup_manager,
    wsapi,
)


def _real_backup_prep_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose ``info`` + ``backup_prep`` are served by the REAL
    component functions against ``hass`` — the seam under test is everything
    between that return value and the consumer's field reads."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        if command_type == backup_module.WS_BACKUP_PREP:
            try:
                result = wsapi._do_backup_prep(hass, dict(kwargs))
            except Exception as exc:  # the REAL component's raise path
                # Mirrors the real WS transport: a handler's raised
                # HomeAssistantError becomes a structured command-error
                # response, which HomeAssistantWebSocketClient.send_command
                # reports to callers as HomeAssistantCommandError.
                raise HomeAssistantCommandError(
                    str(exc), "home_assistant_error"
                ) from exc
            return {"success": True, "result": result}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _client() -> MagicMock:
    client = MagicMock()
    client.base_url = "http://ha.local:8123"
    client.token = "tok"
    client.verify_ssl = False
    return client


def _legacy_ws(
    *, agent_id: str = "backup.local", password: str | None = "legacy-pw"
) -> AsyncMock:
    """The ephemeral legacy WS ``create_backup`` uses for ``backup/generate``
    (the WRITE half, mocked — out of scope here) and, on a component
    fallback, the sequential agents/config probes too."""
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
        "backup/generate": {"success": True, "result": {"backup_job_id": "job-1"}},
    }

    async def _send(command: str, **_kwargs: Any) -> Any:
        if command not in responses:
            raise AssertionError(f"unexpected legacy WS command: {command!r}")
        return responses[command]

    ws.send_command = AsyncMock(side_effect=_send)
    ws.disconnect = AsyncMock()
    return ws


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _generate_call(ws: AsyncMock) -> Any:
    return next(
        c for c in ws.send_command.call_args_list if c.args[0] == "backup/generate"
    )


class TestBackupPrepContract:
    @pytest.mark.asyncio
    async def test_manager_present_serves_create_via_component(self) -> None:
        """The REAL ``_do_backup_prep`` output flows through unchanged into the
        real ``backup/generate`` params — the legacy sequential probes never
        run."""
        hass = FakeHass(
            data={
                "backup": _fake_backup_manager(
                    agents={
                        "backup.local": _fake_backup_agent("local"),
                        "remote.s3": _fake_backup_agent("S3"),
                    },
                    password="pw",
                )
            }
        )
        ws = _real_backup_prep_ws(hass)
        legacy_ws = _legacy_ws()
        client = _client()

        with (
            patch_ws(ws, backup_module),
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
        assert _generate_call(legacy_ws).kwargs["agent_ids"] == ["backup.local"]
        assert _generate_call(legacy_ws).kwargs["password"] == "pw"
        legacy_calls = [c.args[0] for c in legacy_ws.send_command.call_args_list]
        assert "backup/agents/info" not in legacy_calls
        assert "backup/config/info" not in legacy_calls

    @pytest.mark.asyncio
    async def test_hassio_local_preferred_over_backup_local(self) -> None:
        """The component's hassio-over-core preference (mirrored from the
        server's own ``_get_local_backup_agent_id``) survives the seam into
        the real ``backup/generate`` call's ``agent_ids`` / Supervised
        detection."""
        hass = FakeHass(
            data={
                "backup": _fake_backup_manager(
                    agents={
                        "hassio.local": _fake_backup_agent("local"),
                        "backup.local": _fake_backup_agent("local"),
                    },
                    password="pw",
                )
            }
        )
        ws = _real_backup_prep_ws(hass)
        legacy_ws = _legacy_ws()
        client = _client()

        with (
            patch_ws(ws, backup_module),
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
        generate_kwargs = _generate_call(legacy_ws).kwargs
        assert generate_kwargs["agent_ids"] == ["hassio.local"]
        # Supervised detection (agent == "hassio.local") flows through too.
        assert generate_kwargs["include_all_addons"] is True

    @pytest.mark.asyncio
    async def test_password_none_raises_same_error_as_legacy(self) -> None:
        hass = FakeHass(
            data={
                "backup": _fake_backup_manager(
                    agents={"backup.local": _fake_backup_agent("local")},
                    password=None,
                )
            }
        )
        ws = _real_backup_prep_ws(hass)
        client = _client()

        with (
            patch_ws(ws, backup_module),
            patch(
                "ha_mcp.tools.backup.get_connected_ws_client",
                new=AsyncMock(return_value=(_legacy_ws(), None)),
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await backup_module.create_backup(client, name="n")

        assert "No default backup password configured" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_manager_absent_falls_back_to_legacy(self) -> None:
        """The REAL component's manager-absent ``HomeAssistantError`` (routed
        through the transport as a command error) makes the server fall back
        to the legacy sequential probes rather than propagating."""
        hass = FakeHass()  # no "backup" key in .data
        ws = _real_backup_prep_ws(hass)
        legacy_ws = _legacy_ws(agent_id="hassio.local", password="legacy-pw")
        client = _client()

        with (
            patch_ws(ws, backup_module),
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
        assert _generate_call(legacy_ws).kwargs["agent_ids"] == ["hassio.local"]
        assert _generate_call(legacy_ws).kwargs["password"] == "legacy-pw"
