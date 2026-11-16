"""Bad edits an agent can really send, answered by the real component and Core.

Each edit goes through ``ha_config_set_dashboard`` with and without the
component. A refused edit leaves Core's stored dashboard as it was, reaches
Core at most once (never a legacy save after the component refused it), and
no log line from INFO up carries the dashboard's body.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.utils.config_hash import compute_config_hash

from ._command_faults import received
from ._dashboard_edit_routing_core import (
    EDIT,
    READ,
    SAVE,
    lovelace,
    refused,
    stored,
    use_backend,
)

MARKER = "private-dashboard-body"
BODY = {"views": [{"title": "Home", "cards": [{"type": "button", "name": MARKER}]}]}
BACKENDS = pytest.mark.parametrize("backend", ["native", "absent"])


async def _refused_edit(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
    **edit: Any,
) -> tuple[dict[str, Any], dict[str, int]]:
    """The error body, and how many saves, reads and component edits Core got."""
    await lovelace(hass, client, BODY)
    commands = [SAVE, READ]
    if EDIT in hass.data[websocket_api.DOMAIN]:
        commands.append(EDIT)
    with received(hass, *commands) as seen:
        body = await refused(client, config_hash=compute_config_hash(BODY), **edit)
    assert await stored(client) == BODY
    assert MARKER not in str(body)
    logged = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert not [r for r in logged if MARKER in r.getMessage()]
    return body, {
        "save": len(seen[SAVE]),
        "read": len(seen[READ]),
        "edit": len(seen.get(EDIT, [])),
    }


@BACKENDS
@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            {"op": "move", "from": "/views/0", "path": "/views/1"},
            "supported operations",
        ),
        ({"op": "add", "path": "/title"}, "requires a value"),
        ({"op": "remove", "path": 5}, "JSON pointer string"),
        (5, "must be an object"),
        ({"op": "replace", "path": "", "value": []}, "must be an object"),
        ({"op": "replace", "path": "/views/7/title", "value": "x"}, "out of bounds"),
    ],
    ids=[
        "unknown_op",
        "no_value",
        "path_not_string",
        "not_an_object",
        "root_list",
        "no_path",
    ],
)
async def test_a_patch_the_editor_cannot_apply_is_refused_and_written_nowhere(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    backend: str,
    operation: Any,
    message: str,
) -> None:
    """The component refuses it in Core; without the component the server's
    own editor does. Either way nothing is saved, then or as a fallback."""
    use_backend(hass, monkeypatch, backend)

    body, sent = await _refused_edit(hass, core_client, caplog, patch=[operation])

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert message in body["error"]["message"]
    assert (body["reason"], body["write_committed"]) == ("validation_failed", False)
    assert body["action"] == "patch"
    native = backend == "native"
    # The component's refusal never falls back to the server's read and save.
    assert sent == {"save": 0, "read": 0 if native else 1, "edit": 1 if native else 0}


@BACKENDS
async def test_more_than_a_hundred_patch_operations_are_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    backend: str,
) -> None:
    use_backend(hass, monkeypatch, backend)
    check = {"op": "test", "path": "/views/0/title", "value": "Home"}

    body, sent = await _refused_edit(hass, core_client, caplog, patch=[check] * 101)

    assert "at most 100 operations" in body["error"]["message"]
    assert (body["reason"], body["write_committed"]) == ("validation_failed", False)
    native = backend == "native"
    # The component's refusal never falls back to the server's read and save.
    assert sent == {"save": 0, "read": 0 if native else 1, "edit": 1 if native else 0}


@BACKENDS
@pytest.mark.parametrize(
    ("edit", "code", "message"),
    [
        (
            {"patch": '{"op": "remove", "path": "/views/0"}'},
            "VALIDATION_INVALID_PARAMETER",
            "patch must be a list of operations",
        ),
        (
            {"patch": "not json"},
            "VALIDATION_INVALID_PARAMETER",
            "Invalid JSON in patch",
        ),
        ({"config": "5"}, "VALIDATION_FAILED", "JSON object or array, got int"),
        ({"config": "not json"}, "VALIDATION_FAILED", "Invalid JSON in config"),
        (
            {"python_transform": "config['views'][9]['x'] = 1"},
            "VALIDATION_FAILED",
            "raised at runtime: IndexError",
        ),
    ],
    ids=[
        "patch_object",
        "patch_not_json",
        "config_scalar",
        "config_not_json",
        "raises",
    ],
)
async def test_an_edit_that_cannot_be_parsed_or_run_never_reaches_a_save(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    backend: str,
    edit: dict[str, Any],
    code: str,
    message: str,
) -> None:
    use_backend(hass, monkeypatch, backend)

    body, sent = await _refused_edit(hass, core_client, caplog, **edit)

    assert body["error"]["code"] == code
    assert message in body["error"]["message"]
    assert (sent["save"], sent["edit"]) == (0, 0)


@BACKENDS
@pytest.mark.parametrize("result", ["[]", "5"], ids=["list", "number"])
async def test_a_transform_that_leaves_no_object_is_a_validation_error(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    backend: str,
    result: str,
) -> None:
    """The agent's transform is wrong, so the agent is told to fix it; an
    internal error says the server failed."""
    use_backend(hass, monkeypatch, backend)

    body, sent = await _refused_edit(
        hass, core_client, caplog, python_transform=f"config = {result}"
    )

    assert body["error"]["code"].startswith("VALIDATION_")
    assert body["action"] == "python_transform"
    assert (sent["save"], sent["edit"]) == (0, 0)


@BACKENDS
async def test_a_value_json_cannot_encode_is_a_validation_error(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    backend: str,
) -> None:
    """Nothing can be sent, so the agent is told its transform is wrong; the
    edit is not of unknown outcome."""
    use_backend(hass, monkeypatch, backend)

    body, sent = await _refused_edit(
        hass,
        core_client,
        caplog,
        python_transform="config['views'][0]['cards'][0]['tags'] = set()",
    )

    assert (sent["save"], sent["edit"]) == (0, 0)
    assert body["error"]["code"].startswith("VALIDATION_")
    assert body.get("reason") != "write_outcome_unknown"
