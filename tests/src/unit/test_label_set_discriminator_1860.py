"""Unit tests for the create/update discriminator in ``ha_config_set_label``
(issue #1860).

Bug
---
``ha_config_set_label`` chose create vs update purely on whether ``label_id``
was passed (``action = "update" if label_id else "create"``). Passing a
``label_id`` for a not-yet-existing label therefore routed to
``config/label_registry/update``, which HA rejects with an opaque
``Command failed: Unknown error``. The tool's own not-found branch keys on the
substrings ``"not found"`` / ``"doesn't exist"`` and never fired for HA's
actual error text, so callers saw a generic ``SERVICE_CALL_FAILED`` (an agent
in the report retried 23 times before finding the create path).

Fix
---
When ``label_id`` is supplied, verify it exists in the registry BEFORE routing
to update. If it does not, raise ``RESOURCE_NOT_FOUND`` with actionable
guidance ("omit label_id to create"). ``label_registry/create`` cannot honor a
caller-supplied id anyway (HA derives it from ``name``), so the contract stays
strictly update-only rather than upserting to a differently-derived id.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def register_tools(mock_client):
    """Register label tools and return the captured tool callables by name."""
    from ha_mcp.tools.tools_labels import register_label_tools

    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mock_mcp = MagicMock()
    mock_mcp.add_tool = capture_add_tool
    register_label_tools(mock_mcp, mock_client)
    return registered


def _make_ws_handler(existing: list[dict[str, Any]] | None = None):
    """Build a ``send_websocket_message`` side_effect for the label registry.

    ``existing`` seeds the labels returned by ``config/label_registry/list``.
    create/update echo their payload back as a success result.
    """
    labels = existing if existing is not None else []

    async def ws_handler(msg: dict) -> dict:
        msg_type = msg.get("type", "")
        if msg_type == "config/label_registry/list":
            return {"success": True, "result": labels}
        if msg_type == "config/label_registry/create":
            # HA derives the id from the name; mimic the colon->underscore slug.
            derived = msg.get("name", "").lower().replace(":", "_").replace(" ", "_")
            return {
                "success": True,
                "result": {
                    "label_id": derived,
                    **{k: v for k, v in msg.items() if k != "type"},
                },
            }
        if msg_type == "config/label_registry/update":
            return {
                "success": True,
                "result": {k: v for k, v in msg.items() if k != "type"},
            }
        return {"success": True, "result": {}}

    return ws_handler


class TestCreatePath:
    async def test_create_with_only_name_succeeds(self, register_tools, mock_client):
        """No label_id -> create path; no existence check, HA derives the id."""
        mock_client.send_websocket_message = AsyncMock(side_effect=_make_ws_handler())
        result = await register_tools["ha_config_set_label"](name="vendor:tapo")
        assert result["success"] is True
        assert result["label_id"] == "vendor_tapo"


class TestUpdatePath:
    async def test_update_existing_label_succeeds(self, register_tools, mock_client):
        """label_id that exists -> existence check passes, update dispatched."""
        existing = [{"label_id": "vendor_tapo", "name": "vendor:tapo"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(existing)
        )
        result = await register_tools["ha_config_set_label"](
            name="Vendor Tapo", label_id="vendor_tapo", color="blue"
        )
        assert result["success"] is True
        # The mock answers both create and update; assert we actually routed to
        # update (a misroute to create would otherwise pass silently).
        sent_types = [
            c.args[0].get("type")
            for c in mock_client.send_websocket_message.call_args_list
        ]
        assert "config/label_registry/update" in sent_types
        assert "config/label_registry/create" not in sent_types

    async def test_unknown_label_id_returns_not_found_with_guidance(
        self, register_tools, mock_client
    ):
        """label_id absent from the registry -> RESOURCE_NOT_FOUND, not a
        cryptic update failure, and the message points at the create path."""
        existing = [{"label_id": "some_other", "name": "Other"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(existing)
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](name="Ghost", label_id="ghost")
        msg = str(excinfo.value)
        assert "RESOURCE_NOT_FOUND" in msg
        assert "ghost" in msg
        assert "omit label_id" in msg

    async def test_colon_id_returns_not_found_not_unknown_error(
        self, register_tools, mock_client
    ):
        """The exact reporter repro: set_label(label_id='vendor:tapo') for a
        label stored as 'vendor_tapo' must yield the actionable
        RESOURCE_NOT_FOUND, never SERVICE_CALL_FAILED / 'Unknown error'."""
        existing = [{"label_id": "vendor_tapo", "name": "vendor:tapo"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(existing)
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](
                name="vendor:tapo", label_id="vendor:tapo"
            )
        msg = str(excinfo.value)
        assert "RESOURCE_NOT_FOUND" in msg
        assert "SERVICE_CALL_FAILED" not in msg
        assert "Unknown error" not in msg

    async def test_update_dispatch_never_reached_for_unknown_id(
        self, register_tools, mock_client
    ):
        """The guard must fire BEFORE any update WS call is sent — a missing id
        never reaches label_registry/update."""
        existing = [{"label_id": "known", "name": "Known"}]
        handler = AsyncMock(side_effect=_make_ws_handler(existing))
        mock_client.send_websocket_message = handler
        with pytest.raises(ToolError):
            await register_tools["ha_config_set_label"](name="Ghost", label_id="ghost")
        sent_types = [call.args[0].get("type") for call in handler.call_args_list]
        assert "config/label_registry/update" not in sent_types


class TestValidation:
    async def test_empty_label_id_rejected(self, register_tools, mock_client):
        """Empty label_id -> validation error; the existence pre-check must not
        swallow the empty-identifier guard."""
        mock_client.send_websocket_message = AsyncMock(side_effect=_make_ws_handler())
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](name="X", label_id="   ")
        assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value)


class TestGetLabelRefactor:
    """Guards for the shared ``_list_labels`` refactor of ha_config_get_label."""

    async def test_get_label_lists_all(self, register_tools, mock_client):
        existing = [{"label_id": "a", "name": "A"}, {"label_id": "b", "name": "B"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(existing)
        )
        result = await register_tools["ha_config_get_label"]()
        assert result["success"] is True
        assert result["count"] == 2

    async def test_get_label_unknown_returns_not_found(
        self, register_tools, mock_client
    ):
        existing = [{"label_id": "a", "name": "A"}]
        mock_client.send_websocket_message = AsyncMock(
            side_effect=_make_ws_handler(existing)
        )
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_get_label"](label_id="missing")
        assert "RESOURCE_NOT_FOUND" in str(excinfo.value)


class TestMalformedRegistryResponse:
    """A degraded (non-list) registry envelope must fail loudly with
    SERVICE_CALL_FAILED, never degrade unpredictably (adversarial review
    finding; mirrors backup_manager._require_list). Only an empty dict {} would
    (pre-guard) iterate as empty and silently misreport an existing label as
    missing; None, an int, and a non-empty dict would instead crash
    mid-iteration (TypeError / AttributeError). All must raise cleanly."""

    @pytest.mark.parametrize(
        "malformed_result",
        [{}, None, {"unexpected": "dict"}, 42],
    )
    async def test_non_list_result_raises_service_call_failed(
        self, register_tools, mock_client, malformed_result
    ):
        async def bad_handler(msg):
            if msg.get("type") == "config/label_registry/list":
                return {"success": True, "result": malformed_result}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=bad_handler)
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](name="X", label_id="known")
        msg = str(excinfo.value)
        assert "SERVICE_CALL_FAILED" in msg
        assert "RESOURCE_NOT_FOUND" not in msg

    async def test_list_failure_raises_service_call_failed(
        self, register_tools, mock_client
    ):
        """A failed registry-list response (success=False) surfaces
        SERVICE_CALL_FAILED via the shared _list_labels error path — now reached
        by both get and set."""

        async def bad_handler(msg):
            if msg.get("type") == "config/label_registry/list":
                return {"success": False, "error": "Failed to get labels"}
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=bad_handler)
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](name="X", label_id="known")
        assert "SERVICE_CALL_FAILED" in str(excinfo.value)

    async def test_success_envelope_omitting_result_key_raises(
        self, register_tools, mock_client
    ):
        """A success response that omits the ``result`` key entirely must also
        raise SERVICE_CALL_FAILED — not default to [] and misreport an existing
        label_id as missing."""

        async def bad_handler(msg):
            if msg.get("type") == "config/label_registry/list":
                return {"success": True}  # no "result" key
            return {"success": True, "result": {}}

        mock_client.send_websocket_message = AsyncMock(side_effect=bad_handler)
        with pytest.raises(ToolError) as excinfo:
            await register_tools["ha_config_set_label"](name="X", label_id="known")
        msg = str(excinfo.value)
        assert "SERVICE_CALL_FAILED" in msg
        assert "RESOURCE_NOT_FOUND" not in msg
