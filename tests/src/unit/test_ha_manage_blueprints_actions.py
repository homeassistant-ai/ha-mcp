"""Action dispatch and error taxonomy for ``ha_manage_blueprints`` (#2329).

The consolidated tool answers five actions over the same WebSocket client, so
these tests drive a spy whose ``send_websocket_message`` dispatches on the
frame ``type`` (the pattern from
``test_ha_manage_blueprints_component_routing.py``) and pin what the tool sends
and what it raises:

- the ``confirm=True`` gate on ``delete`` and its existence pre-check,
- ``BlueprintInUse`` becoming ``RESOURCE_LOCKED`` with the consumers
  ``search/related`` names — and the degraded variant when that lookup itself
  fails,
- the post-delete state verification core's empty ack cannot provide,
- ``substitute``'s message-only error taxonomy (core answers every failure with
  the same ``unknown_error`` code),
- and the per-action required parameters.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_blueprints import register_blueprint_tools

_PATH = "user/motion.yaml"
_METADATA = {
    "metadata": {
        "name": "Motion Light",
        "description": "Turn on a light on motion.",
        "domain": "automation",
        "input": {"motion_sensor": {"name": "Motion Sensor"}},
    }
}


def _error_payload(exc: ToolError) -> dict[str, Any]:
    """Unwrap the structured error ``raise_tool_error`` serialises into the message.

    ``create_error_response`` nests code/message/suggestions under ``error``
    and splices the ``context`` fields onto the top level, so the whole payload
    is returned.
    """
    payload = json.loads(str(exc))
    assert isinstance(payload, dict)
    assert isinstance(payload["error"], dict)
    return payload


class SpyClient:
    """Dispatch-on-``type`` WebSocket spy.

    ``responses`` maps a frame type to either a response dict or a callable
    taking the frame; a type with no entry raises, so an unexpected frame is a
    test failure rather than a silent default.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = True
        self.responses = responses
        self.sent: list[dict[str, Any]] = []

    async def send_websocket_message(self, msg: dict[str, Any]) -> Any:
        self.sent.append(msg)
        frame_type = msg.get("type")
        if frame_type not in self.responses:
            raise AssertionError(f"unexpected WebSocket frame: {frame_type}")
        response = self.responses[frame_type]
        if callable(response):
            return response(msg)
        if isinstance(response, BaseException):
            raise response
        return response

    def frames(self, frame_type: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("type") == frame_type]


def _build_tool(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_blueprint_tools(mcp, client)
    return registered["ha_manage_blueprints"]


def _listing(*paths: str) -> dict[str, Any]:
    return {"success": True, "result": {p: dict(_METADATA) for p in paths}}


@pytest.fixture(autouse=True)
def _no_auto_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the auto-backup decorator out of these cases.

    ``action="delete"`` is the one action the decorator does not skip; its own
    behaviour is covered in ``test_backup_manager.py``. Here the master toggle
    is forced off so the delete cases exercise the tool alone.
    """

    class _Settings:
        enable_auto_backup = False

    monkeypatch.setattr("ha_mcp.tools.auto_backup.get_global_settings", _Settings)


# ------------------------------------------------------------------ validation


@pytest.mark.asyncio
async def test_invalid_domain_rejected() -> None:
    """The explicit domain check (and its error code) is unchanged."""
    client = SpyClient({})
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="list", domain="light")

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "Invalid domain 'light'" in error["message"]
    assert client.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "missing"),
    [("get", "path"), ("import", "url"), ("delete", "path"), ("substitute", "path")],
)
async def test_required_parameter_missing(action: str, missing: str) -> None:
    client = SpyClient({})
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action=action)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "VALIDATION_MISSING_PARAMETER"
    assert f"'{missing}' is required for action='{action}'" in error["message"]
    assert client.sent == []


# ---------------------------------------------------------------------- delete


@pytest.mark.asyncio
async def test_delete_requires_confirm() -> None:
    """No confirm → refused before any frame is sent, so nothing is deleted."""
    client = SpyClient({})
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error["message"] == (
        f"Deletion not confirmed. Set confirm=True to delete blueprint '{_PATH}'."
    )
    assert client.sent == []


@pytest.mark.asyncio
async def test_delete_not_found() -> None:
    """The existence pre-check reuses ``get``'s not-found error."""
    client = SpyClient({"blueprint/list": _listing("other/thing.yaml")})
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH, confirm=True)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert payload["available_blueprints"] == ["other/thing.yaml"]
    assert client.frames("blueprint/delete") == []


@pytest.mark.asyncio
async def test_delete_success_verifies_removal() -> None:
    """Success re-reads the store: core's ack carries no result to trust."""
    listings = [_listing(_PATH), _listing()]
    client = SpyClient(
        {
            "blueprint/list": lambda _m: listings.pop(0),
            "blueprint/delete": {"success": True, "result": None},
        }
    )
    tool = _build_tool(client)

    resp = await tool(action="delete", path=_PATH, confirm=True)

    assert resp == {
        "success": True,
        "domain": "automation",
        "path": _PATH,
        "message": "Blueprint deleted.",
    }
    assert client.frames("blueprint/delete") == [
        {"type": "blueprint/delete", "domain": "automation", "path": _PATH}
    ]
    # Pre-check + post-verification.
    assert len(client.frames("blueprint/list")) == 2


@pytest.mark.asyncio
async def test_delete_still_present_after_success_is_an_error() -> None:
    """A blueprint still listed after a 'successful' delete must not report success."""
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "blueprint/delete": {"success": True, "result": None},
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH, confirm=True)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "still installed" in error["message"]


@pytest.mark.asyncio
async def test_delete_generic_failure_is_service_call_failed() -> None:
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "blueprint/delete": {
                "success": False,
                "error": "Command failed: Permission denied",
            },
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH, confirm=True)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert error["message"] == "Command failed: Permission denied"
    assert client.frames("search/related") == []


@pytest.mark.asyncio
async def test_delete_in_use_names_consumers() -> None:
    """``Blueprint in use`` → RESOURCE_LOCKED carrying the reference graph's answer."""
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "blueprint/delete": {
                "success": False,
                "error": "Command failed: Blueprint in use",
            },
            "search/related": {
                "success": True,
                "result": {
                    "automation": ["automation.hall_light", "automation.porch_light"],
                    "config_entry": [],
                },
            },
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH, confirm=True)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "RESOURCE_LOCKED"
    assert payload["in_use_by"] == [
        "automation.hall_light",
        "automation.porch_light",
    ]
    assert "automation.hall_light" in error["message"]
    assert client.frames("search/related") == [
        {
            "type": "search/related",
            "item_type": "automation_blueprint",
            "item_id": _PATH,
        }
    ]
    joined = " ".join(error["suggestions"])
    assert "ha_config_remove_automation" in joined
    assert 'action="substitute"' in joined
    assert 'action="delete"' in joined


@pytest.mark.asyncio
async def test_delete_in_use_when_reference_lookup_fails() -> None:
    """The lock still surfaces when ``search/related`` cannot be consulted."""
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "blueprint/delete": {
                "success": False,
                "error": "Command failed: Blueprint in use",
            },
            "search/related": {"success": False, "error": "Unknown command."},
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH, confirm=True)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "RESOURCE_LOCKED"
    assert payload["in_use_by"] == []
    assert any("ha_search" in s for s in error["suggestions"])


@pytest.mark.asyncio
async def test_delete_in_use_when_reference_lookup_raises() -> None:
    """A transport failure on the enrichment must not mask the lock."""
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "blueprint/delete": {
                "success": False,
                "error": "Command failed: Blueprint in use",
            },
            "search/related": ConnectionError("socket gone"),
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH, confirm=True)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "RESOURCE_LOCKED"
    assert payload["in_use_by"] == []


@pytest.mark.asyncio
async def test_delete_script_blueprint_uses_script_item_type() -> None:
    """The reference lookup's item_type follows the blueprint domain."""
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "blueprint/delete": {
                "success": False,
                "error": "Command failed: Blueprint in use",
            },
            "search/related": {
                "success": True,
                "result": {"script": ["script.bedtime"]},
            },
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", domain="script", path=_PATH, confirm=True)

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert payload["in_use_by"] == ["script.bedtime"]
    assert client.frames("search/related")[0]["item_type"] == "script_blueprint"
    assert any("ha_config_remove_script" in s for s in error["suggestions"])


# ------------------------------------------------------------------ substitute


@pytest.mark.asyncio
async def test_substitute_returns_standalone_config() -> None:
    rendered = {
        "trigger": [{"platform": "state", "entity_id": "binary_sensor.hall"}],
        "action": [{"service": "light.turn_on"}],
    }
    client = SpyClient(
        {
            "blueprint/substitute": {
                "success": True,
                "result": {"substituted_config": rendered},
            }
        }
    )
    tool = _build_tool(client)

    resp = await tool(
        action="substitute", path=_PATH, input={"motion_sensor": "binary_sensor.hall"}
    )

    assert resp == {
        "success": True,
        "domain": "automation",
        "path": _PATH,
        "config": rendered,
    }
    assert client.frames("blueprint/substitute") == [
        {
            "type": "blueprint/substitute",
            "domain": "automation",
            "path": _PATH,
            "input": {"motion_sensor": "binary_sensor.hall"},
        }
    ]


@pytest.mark.asyncio
async def test_substitute_defaults_input_to_empty_dict() -> None:
    """An omitted ``input`` still satisfies core's required key."""
    client = SpyClient(
        {
            "blueprint/substitute": {
                "success": True,
                "result": {"substituted_config": {"action": []}},
            }
        }
    )
    tool = _build_tool(client)

    await tool(action="substitute", path=_PATH)

    assert client.frames("blueprint/substitute")[0]["input"] == {}


@pytest.mark.asyncio
async def test_substitute_missing_input_is_validation_failed() -> None:
    client = SpyClient(
        {
            "blueprint/substitute": {
                "success": False,
                "error": "Command failed: Missing input motion_sensor",
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="substitute", path=_PATH, input={})

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "Missing input motion_sensor" in error["message"]


@pytest.mark.asyncio
async def test_substitute_failed_to_load_is_not_found() -> None:
    client = SpyClient(
        {
            "blueprint/substitute": {
                "success": False,
                "error": (
                    "Command failed: Failed to load blueprint: "
                    "Unable to find user/motion.yaml"
                ),
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="substitute", path=_PATH, input={})

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_substitute_other_failure_is_service_call_failed() -> None:
    client = SpyClient(
        {
            "blueprint/substitute": {
                "success": False,
                "error": "Command failed: Unsupported domain",
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="substitute", path=_PATH, input={})

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert error["message"] == "Command failed: Unsupported domain"


# ------------------------------------------------------------------ list/import


@pytest.mark.asyncio
async def test_list_response_shape_unchanged() -> None:
    client = SpyClient({"blueprint/list": _listing(_PATH)})
    tool = _build_tool(client)

    resp = await tool(action="list", domain="automation")

    assert resp["success"] is True
    assert resp["domain"] == "automation"
    assert resp["count"] == 1
    assert resp["blueprints"][0]["path"] == _PATH
    assert resp["blueprints"][0]["description"] == "Turn on a light on motion."


@pytest.mark.asyncio
async def test_import_saves_and_reports_the_installed_path() -> None:
    client = SpyClient(
        {
            "blueprint/import": {
                "success": True,
                "result": {
                    "suggested_filename": "user/motion",
                    "raw_data": "blueprint:\n  name: Motion Light\n",
                    "blueprint": {
                        "metadata": {
                            "domain": "automation",
                            "name": "Motion Light",
                            "description": "Turn on a light on motion.",
                        }
                    },
                    "validation_errors": None,
                    "exists": False,
                },
            },
            "blueprint/save": {
                "success": True,
                "result": {"overrides_existing": False},
            },
        }
    )
    tool = _build_tool(client)

    resp = await tool(action="import", url="https://example.com/bp.yaml")

    assert resp["success"] is True
    assert resp["imported_blueprint"] == {
        "path": "user/motion.yaml",
        "domain": "automation",
        "name": "Motion Light",
        "description": "Turn on a light on motion.",
    }
    assert resp["overrides_existing"] is False
    save = client.frames("blueprint/save")[0]
    assert save["path"] == "user/motion.yaml"
    assert save["source_url"] == "https://example.com/bp.yaml"
    # allow_override is only sent when actually overwriting (older HA schemas
    # reject the unknown key).
    assert "allow_override" not in save


@pytest.mark.asyncio
async def test_import_existing_without_overwrite_is_rejected() -> None:
    client = SpyClient(
        {
            "blueprint/import": {
                "success": True,
                "result": {
                    "suggested_filename": "user/motion",
                    "raw_data": "blueprint:\n  name: Motion Light\n",
                    "blueprint": {"metadata": {"domain": "automation"}},
                    "exists": True,
                },
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="import", url="https://example.com/bp.yaml")

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "RESOURCE_ALREADY_EXISTS"
    assert client.frames("blueprint/save") == []


@pytest.mark.asyncio
async def test_import_rejects_non_http_url() -> None:
    client = SpyClient({})
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="import", url="ftp://example.com/bp.yaml")

    payload = _error_payload(exc.value)
    error = payload["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert client.sent == []
