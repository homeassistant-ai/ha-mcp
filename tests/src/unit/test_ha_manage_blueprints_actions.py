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

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import tools_blueprints
from ha_mcp.tools.blueprint_sources import BlueprintSource
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


_RELATED_EMPTY: dict[str, Any] = {"success": True, "result": {}}


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
    [
        ("get", "path"),
        ("import", "url"),
        ("save", "path"),
        ("delete", "path"),
        ("substitute", "path"),
    ],
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
async def test_unconfirmed_delete_captures_no_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With auto-backup ON, an unconfirmed delete must still capture nothing.

    The refusal changes nothing, so a snapshot for it is pure cost — a
    component file read, and with no component an outbound re-fetch of the
    blueprint's third-party source_url — paid before the tool says no.
    """
    captured: list[tuple[str, str]] = []

    class _FakeMgr:
        async def maybe_snapshot(
            self, domain: str, entity_id: str, **_kwargs: Any
        ) -> None:
            captured.append((domain, entity_id))

    class _Settings:
        enable_auto_backup = True

    monkeypatch.setattr("ha_mcp.tools.auto_backup.get_global_settings", _Settings)
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
    )

    client = SpyClient({})
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH)

    assert _error_payload(exc.value)["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert captured == []
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
        "data": {
            "domain": "automation",
            "path": _PATH,
            "message": "Blueprint deleted.",
        },
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
    """``Blueprint in use`` → RESOURCE_LOCKED carrying the reference graph's answer.

    Only the bucket named by the blueprint domain counts. ``search/related``
    returns an open dict, so the extra buckets here stand in for related items
    core could start returning — unioning them would report ``light.hall`` as a
    blueprint consumer.
    """
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
                    "light": ["light.hall"],
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
    assert "light.hall" not in str(payload), (
        "a non-automation bucket leaked into the blueprint's consumer list"
    )
    assert "automation.hall_light" in error["message"]
    # The lookup only enriches an error already being raised, so it carries a
    # short leash rather than send_command's 30s default.
    assert client.frames("search/related") == [
        {
            "type": "search/related",
            "item_type": "automation_blueprint",
            "item_id": _PATH,
            "_wait_timeout": 5.0,
        }
    ]
    joined = " ".join(error["suggestions"])
    assert "ha_config_remove_automation" in joined
    assert 'action="substitute"' in joined
    assert 'action="delete"' in joined


@pytest.mark.asyncio
async def test_delete_in_use_with_no_matching_bucket() -> None:
    """The graph answered but named no consumer of this domain.

    Home Assistant still refused the delete, so the lock stands; the message
    just cannot name anyone.
    """
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "blueprint/delete": {
                "success": False,
                "error": "Command failed: Blueprint in use",
            },
            "search/related": {"success": True, "result": {"config_entry": []}},
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="delete", path=_PATH, confirm=True)

    payload = _error_payload(exc.value)
    assert payload["error"]["code"] == "RESOURCE_LOCKED"
    assert payload["in_use_by"] == []
    # The lookup DID answer, so no "use ha_search instead" suggestion is added.
    assert not any("ha_search" in s for s in payload["error"]["suggestions"])


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
        "data": {"domain": "automation", "path": _PATH, "config": rendered},
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
    data = resp["data"]
    assert data["domain"] == "automation"
    assert data["count"] == 1
    assert data["blueprints"][0]["path"] == _PATH
    assert data["blueprints"][0]["description"] == "Turn on a light on motion."


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
    assert resp["data"]["imported_blueprint"] == {
        "path": "user/motion.yaml",
        "domain": "automation",
        "name": "Motion Light",
        "description": "Turn on a light on motion.",
    }
    assert resp["data"]["overrides_existing"] is False
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
async def test_import_failure_keeps_the_network_suggestions() -> None:
    """Importing is the only action that makes HA reach the internet, so the
    connectivity remedies belong on this path."""
    client = SpyClient(
        {
            "blueprint/import": {
                "success": False,
                "error": "Command failed: Cannot connect to host example.com",
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="import", url="https://example.com/bp.yaml")

    payload = _error_payload(exc.value)
    assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
    joined = " ".join(payload["error"]["suggestions"])
    assert "internet access" in joined
    assert "different source" in joined


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


# ------------------------------------------------------------------------ save

_SAVE_YAML = "blueprint:\n  name: Motion Light\n  domain: automation\n"


@pytest.mark.asyncio
async def test_save_requires_yaml() -> None:
    client = SpyClient({})
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="save", path=_PATH)

    error = _error_payload(exc.value)["error"]
    assert error["code"] == "VALIDATION_MISSING_PARAMETER"
    assert "'yaml' is required for action='save'" in error["message"]
    assert client.sent == []


@pytest.mark.asyncio
async def test_save_new_path_sends_neither_source_url_nor_override() -> None:
    """A hand-authored blueprint carries no source_url: the key must be absent
    (core's ``vol.Optional``), not ``None``, and allow_override is not sent."""
    client = SpyClient(
        {"blueprint/save": {"success": True, "result": {"overrides_existing": False}}}
    )
    tool = _build_tool(client)

    resp = await tool(action="save", path=_PATH, yaml=_SAVE_YAML)

    assert resp == {
        "success": True,
        "data": {
            "domain": "automation",
            "path": _PATH,
            "overrides_existing": False,
            "message": "Blueprint saved.",
        },
    }
    assert client.frames("blueprint/save") == [
        {
            "type": "blueprint/save",
            "domain": "automation",
            "path": _PATH,
            "yaml": _SAVE_YAML,
        }
    ]


@pytest.mark.asyncio
async def test_save_overwrite_stamps_source_url_and_reports_the_reload() -> None:
    client = SpyClient(
        {"blueprint/save": {"success": True, "result": {"overrides_existing": True}}}
    )
    tool = _build_tool(client)

    resp = await tool(
        action="save",
        domain="script",
        path=_PATH,
        yaml=_SAVE_YAML,
        overwrite=True,
        source_url="https://example.com/bp.yaml",
    )

    assert resp["data"]["overrides_existing"] is True
    assert "reloaded" in resp["data"]["message"]
    assert client.frames("blueprint/save") == [
        {
            "type": "blueprint/save",
            "domain": "script",
            "path": _PATH,
            "yaml": _SAVE_YAML,
            "source_url": "https://example.com/bp.yaml",
            "allow_override": True,
        }
    ]


@pytest.mark.asyncio
async def test_save_existing_without_overwrite_is_already_exists() -> None:
    client = SpyClient(
        {
            "blueprint/save": {
                "success": False,
                "error": {"code": "already_exists", "message": "File already exists"},
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="save", path=_PATH, yaml=_SAVE_YAML)

    error = _error_payload(exc.value)["error"]
    assert error["code"] == "RESOURCE_ALREADY_EXISTS"
    assert error["message"] == "File already exists"
    assert "overwrite=True" in " ".join(error["suggestions"])


@pytest.mark.asyncio
async def test_save_invalid_blueprint_is_validation_failed() -> None:
    client = SpyClient(
        {
            "blueprint/save": {
                "success": False,
                "error": {
                    "code": "invalid_format",
                    "message": (
                        "Invalid blueprint: required key not provided "
                        "@ data['blueprint']"
                    ),
                },
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="save", path=_PATH, yaml="not: [a blueprint")

    error = _error_payload(exc.value)["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "required key not provided" in error["message"]


@pytest.mark.asyncio
async def test_save_invalid_yaml_classifies_by_the_clients_error_code() -> None:
    """The REST client flattens core's error to a string and carries the
    structured code in a sibling ``error_code`` key — that key, not the
    message text, must drive the mapping (a YAML parse error mentions no
    "invalid")."""
    client = SpyClient(
        {
            "blueprint/save": {
                "success": False,
                "error": (
                    "Command failed: while parsing a flow sequence in "
                    "<unicode string>, line 1, column 6: expected a comma or a "
                    "closing bracket"
                ),
                "error_code": "invalid_format",
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="save", path=_PATH, yaml="not: [a blueprint")

    error = _error_payload(exc.value)["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "while parsing a flow sequence" in error["message"]


@pytest.mark.asyncio
async def test_save_other_failure_is_service_call_failed() -> None:
    client = SpyClient(
        {
            "blueprint/save": {
                "success": False,
                "error": "Command failed: [Errno 13] Permission denied",
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="save", path=_PATH, yaml=_SAVE_YAML)

    error = _error_payload(exc.value)["error"]
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "Permission denied" in error["message"]


# --------------------------------------------------------------- get + ladder


class _LadderSpy:
    """Stand-in for ``resolve_blueprint_source`` recording what ``get`` asks."""

    def __init__(self, found: BlueprintSource) -> None:
        self.found = found
        self.calls: list[tuple[str, str, Any]] = []

    async def __call__(
        self, client: Any, domain: str, path: str, *, source_url: Any
    ) -> BlueprintSource:
        self.calls.append((domain, path, source_url))
        return self.found


def _patch_ladder(
    monkeypatch: pytest.MonkeyPatch, found: BlueprintSource
) -> _LadderSpy:
    spy = _LadderSpy(found)
    monkeypatch.setattr(tools_blueprints, "resolve_blueprint_source", spy)
    return spy


@pytest.mark.asyncio
async def test_get_surfaces_yaml_and_its_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = _listing(_PATH)
    listing["result"][_PATH]["metadata"]["source_url"] = "https://example.com/bp.yaml"
    client = SpyClient({"blueprint/list": listing, "search/related": _RELATED_EMPTY})
    spy = _patch_ladder(
        monkeypatch,
        BlueprintSource(
            text=_SAVE_YAML,
            config={"blueprint": {"name": "Motion Light"}},
            source="component",
            warning=None,
        ),
    )
    tool = _build_tool(client)

    resp = await tool(action="get", path=_PATH)

    assert resp["data"]["yaml"] == _SAVE_YAML
    assert resp["data"]["yaml_source"] == "component"
    assert resp["data"]["config"] == {"blueprint": {"name": "Motion Light"}}
    # warnings stay top-level, never nested in data (style guide).
    assert "warnings" not in resp
    # The ladder is handed the recorded source_url so its last tier can run.
    assert spy.calls == [("automation", _PATH, "https://example.com/bp.yaml")]


@pytest.mark.asyncio
async def test_get_warns_when_the_yaml_is_a_source_url_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = _listing(_PATH)
    listing["result"][_PATH]["metadata"]["source_url"] = "https://example.com/bp.yaml"
    client = SpyClient({"blueprint/list": listing, "search/related": _RELATED_EMPTY})
    _patch_ladder(
        monkeypatch,
        BlueprintSource(text=_SAVE_YAML, config={}, source="source_url", warning=None),
    )
    tool = _build_tool(client)

    resp = await tool(action="get", path=_PATH)

    assert resp["data"]["yaml_source"] == "source_url"
    assert len(resp["warnings"]) == 1
    assert "https://example.com/bp.yaml" in resp["warnings"][0]
    assert "not read from the installed file" in resp["warnings"][0]


@pytest.mark.asyncio
async def test_get_omits_yaml_keys_when_no_tier_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SpyClient(
        {"blueprint/list": _listing(_PATH), "search/related": _RELATED_EMPTY}
    )
    _patch_ladder(monkeypatch, BlueprintSource(None, None, None, None))
    tool = _build_tool(client)

    resp = await tool(action="get", path=_PATH)

    assert resp["success"] is True
    assert resp["data"]["metadata"]["name"] == "Motion Light"
    for key in ("yaml", "yaml_source", "config"):
        assert key not in resp["data"], key
    assert "warnings" not in resp


@pytest.mark.asyncio
async def test_get_keeps_the_component_warning_when_nothing_else_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SpyClient(
        {"blueprint/list": _listing(_PATH), "search/related": _RELATED_EMPTY}
    )
    _patch_ladder(
        monkeypatch, BlueprintSource(None, None, None, "component could not read it")
    )
    tool = _build_tool(client)

    resp = await tool(action="get", path=_PATH)

    assert resp["warnings"] == ["component could not read it"]
    assert "yaml" not in resp


# ------------------------------------------------- review round 1 (#2356)


@pytest.mark.asyncio
async def test_import_ignores_the_domain_parameter() -> None:
    """The blueprint file declares its own domain, so a stray value must not
    refuse an otherwise valid import (Codex P3)."""
    client = SpyClient(
        {
            "blueprint/import": {
                "success": True,
                "result": {
                    "suggested_filename": "user/motion",
                    "raw_data": "blueprint:\n  name: Motion Light\n",
                    "blueprint": {"metadata": {"domain": "automation", "name": "M"}},
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

    resp = await tool(
        action="import", url="https://example.com/bp.yaml", domain="light"
    )

    assert resp["success"] is True
    assert client.frames("blueprint/save")[0]["domain"] == "automation"


@pytest.mark.asyncio
async def test_import_tolerates_a_null_blueprint_field() -> None:
    """``"blueprint": None`` in a successful import result is handled by the
    missing-filename gate, not an AttributeError (CodeRabbit)."""
    client = SpyClient(
        {
            "blueprint/import": {
                "success": True,
                "result": {"suggested_filename": "", "raw_data": "", "blueprint": None},
            }
        }
    )
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="import", url="https://example.com/bp.yaml")

    error = _error_payload(exc.value)["error"]
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "no filename or YAML data" in error["message"]


@pytest.mark.asyncio
async def test_overwriting_import_snapshots_the_installed_blueprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-import over an installed file is a write the decorator cannot see
    (the path is only known after blueprint/import), so it captures inline
    (Codex P2)."""
    captured: list[tuple[str, str, str | None]] = []

    class _FakeMgr:
        async def maybe_snapshot(
            self, domain: str, entity_id: str, *, tool_name: str | None = None, **_: Any
        ) -> None:
            captured.append((domain, entity_id, tool_name))

    class _Settings:
        enable_auto_backup = True

    monkeypatch.setattr("ha_mcp.tools.blueprint_write.get_global_settings", _Settings)
    monkeypatch.setattr(
        "ha_mcp.tools.blueprint_write.get_backup_manager", lambda _c, _s: _FakeMgr()
    )
    client = SpyClient(
        {
            "blueprint/import": {
                "success": True,
                "result": {
                    "suggested_filename": "user/motion",
                    "raw_data": "blueprint:\n  name: Motion Light\n",
                    "blueprint": {"metadata": {"domain": "script", "name": "M"}},
                    "validation_errors": None,
                    "exists": True,
                },
            },
            "blueprint/save": {"success": True, "result": {"overrides_existing": True}},
        }
    )
    tool = _build_tool(client)

    await tool(action="import", url="https://example.com/bp.yaml", overwrite=True)

    assert captured == [
        ("blueprint_script", "user/motion.yaml", "ha_manage_blueprints")
    ]


@pytest.mark.asyncio
async def test_every_overwriting_import_attempts_the_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core reports ``exists=false`` for an installed file that failed to load,
    and ``allow_override`` replaces that file just the same — so the capture is
    attempted whenever overwrite is set, not only when core says the path
    exists. A destination that truly is absent has nothing to read, and the
    backup manager skips it on its own."""
    captured: list[tuple[str, str]] = []

    class _FakeMgr:
        async def maybe_snapshot(
            self, domain: str, entity_id: str, **_kwargs: Any
        ) -> None:
            captured.append((domain, entity_id))

    class _Settings:
        enable_auto_backup = True

    monkeypatch.setattr("ha_mcp.tools.blueprint_write.get_global_settings", _Settings)
    monkeypatch.setattr(
        "ha_mcp.tools.blueprint_write.get_backup_manager", lambda _c, _s: _FakeMgr()
    )
    client = SpyClient(
        {
            "blueprint/import": {
                "success": True,
                "result": {
                    "suggested_filename": "user/motion",
                    "raw_data": _SAVE_YAML,
                    "blueprint": {"metadata": {"domain": "automation", "name": "M"}},
                    "validation_errors": None,
                    # The load-failure shape: core cannot read it, so it reports
                    # the path as absent even though the file is on disk.
                    "exists": False,
                },
            },
            "blueprint/save": {"success": True, "result": {"overrides_existing": True}},
        }
    )
    tool = _build_tool(client)

    await tool(action="import", url="https://example.com/bp.yaml", overwrite=True)

    assert captured == [("blueprint_automation", "user/motion.yaml")]


@pytest.mark.asyncio
async def test_import_without_overwrite_captures_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain import cannot replace anything — core refuses the save without
    ``allow_override`` — so there is nothing to snapshot."""
    captured: list[Any] = []

    class _FakeMgr:
        async def maybe_snapshot(self, *args: Any, **kwargs: Any) -> None:
            captured.append(args)

    class _Settings:
        enable_auto_backup = True

    monkeypatch.setattr("ha_mcp.tools.blueprint_write.get_global_settings", _Settings)
    monkeypatch.setattr(
        "ha_mcp.tools.blueprint_write.get_backup_manager", lambda _c, _s: _FakeMgr()
    )
    client = SpyClient(
        {
            "blueprint/import": {
                "success": True,
                "result": {
                    "suggested_filename": "user/motion",
                    "raw_data": _SAVE_YAML,
                    "blueprint": {"metadata": {"domain": "automation", "name": "M"}},
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

    await tool(action="import", url="https://example.com/bp.yaml")

    assert captured == []


_FUTURE_YAML = (
    "blueprint:\n  name: Future\n  domain: automation\n"
    "  homeassistant:\n    min_version: 9999.1.0\n"
)


def _client_with_version(version: str, responses: dict[str, Any]) -> SpyClient:
    client = SpyClient(responses)

    async def get_config() -> dict[str, Any]:
        return {"version": version}

    client.get_config = get_config  # type: ignore[attr-defined]
    return client


@pytest.mark.asyncio
async def test_save_refuses_a_blueprint_this_home_assistant_cannot_run() -> None:
    """blueprint/save skips the min_version gate import applies, so the tool
    applies it itself (Codex P1). Wording mirrors core's validate()."""
    client = _client_with_version("2026.8.2", {})
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(action="save", path=_PATH, yaml=_FUTURE_YAML)

    error = _error_payload(exc.value)["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "Requires at least Home Assistant 9999.1.0" in error["message"]
    assert client.frames("blueprint/save") == []


@pytest.mark.asyncio
async def test_save_proceeds_when_min_version_is_satisfied() -> None:
    client = _client_with_version(
        "9999.2.0",
        {"blueprint/save": {"success": True, "result": {"overrides_existing": False}}},
    )
    tool = _build_tool(client)

    resp = await tool(action="save", path=_PATH, yaml=_FUTURE_YAML)

    assert resp["success"] is True
    assert len(client.frames("blueprint/save")) == 1


@pytest.mark.asyncio
async def test_save_without_min_version_never_asks_for_the_version() -> None:
    client = SpyClient(
        {"blueprint/save": {"success": True, "result": {"overrides_existing": False}}}
    )
    # No get_config on the spy: reaching for it would raise AttributeError.
    tool = _build_tool(client)

    resp = await tool(action="save", path=_PATH, yaml=_SAVE_YAML)

    assert resp["success"] is True


@pytest.mark.asyncio
async def test_save_proceeds_when_the_version_lookup_fails() -> None:
    """The min_version check exists to refuse a blueprint this Home Assistant
    cannot run; a transport failure while asking for the version must not block
    a save core itself would accept."""
    client = SpyClient(
        {"blueprint/save": {"success": True, "result": {"overrides_existing": False}}}
    )

    async def get_config() -> dict[str, Any]:
        raise HomeAssistantConnectionError("socket gone")

    client.get_config = get_config  # type: ignore[attr-defined]
    tool = _build_tool(client)

    resp = await tool(action="save", path=_PATH, yaml=_FUTURE_YAML)

    assert resp["success"] is True
    assert len(client.frames("blueprint/save")) == 1


# ------------------------------------------------------- get: used_by (#2356)


_RELATED_OK = {
    "success": True,
    "result": {
        "automation": ["automation.hall", "automation.porch"],
        "config_entry": [],
    },
}


@pytest.mark.asyncio
async def test_get_reports_the_automations_using_the_blueprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UI's "Show automations using this blueprint", answerable without
    attempting a delete first."""
    client = SpyClient(
        {"blueprint/list": _listing(_PATH), "search/related": _RELATED_OK}
    )
    _patch_ladder(monkeypatch, BlueprintSource(None, None, None, None))
    tool = _build_tool(client)

    resp = await tool(action="get", path=_PATH)

    assert resp["data"]["used_by"] == ["automation.hall", "automation.porch"]
    assert client.frames("search/related")[0]["item_type"] == "automation_blueprint"
    assert "warnings" not in resp


@pytest.mark.asyncio
async def test_get_reports_an_unused_blueprint_as_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "search/related": {"success": True, "result": {"config_entry": ["x"]}},
        }
    )
    _patch_ladder(monkeypatch, BlueprintSource(None, None, None, None))
    tool = _build_tool(client)

    resp = await tool(action="get", path=_PATH)

    assert resp["data"]["used_by"] == []
    assert "warnings" not in resp


@pytest.mark.asyncio
async def test_get_omits_used_by_when_the_lookup_cannot_be_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent key must never read as "nothing uses this" — say so instead."""
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "search/related": {"success": False, "error": "Unknown command."},
        }
    )
    _patch_ladder(monkeypatch, BlueprintSource(None, None, None, None))
    tool = _build_tool(client)

    resp = await tool(action="get", path=_PATH)

    assert "used_by" not in resp["data"]
    assert any("reference lookup" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_get_used_by_follows_the_script_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SpyClient(
        {
            "blueprint/list": _listing(_PATH),
            "search/related": {
                "success": True,
                "result": {"script": ["script.bedtime"]},
            },
        }
    )
    _patch_ladder(monkeypatch, BlueprintSource(None, None, None, None))
    tool = _build_tool(client)

    resp = await tool(action="get", domain="script", path=_PATH)

    assert resp["data"]["used_by"] == ["script.bedtime"]
    assert client.frames("search/related")[0]["item_type"] == "script_blueprint"
