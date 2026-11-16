"""A refused dashboard edit names the caller's mode, its outcome and a next
step that fits it, with or without the component.

Each refusal comes from Core's own ``lovelace``, from the component on it, or
from the server's checks before anything is sent; the dashboards are Core's
storage and YAML dashboards on a real ``hass``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import dump

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.utils.config_hash import compute_config_hash

from ._client_spies import sent_messages
from ._command_faults import received
from ._config_dir_yaml import write_config_files
from ._dashboard_edit_routing_core import (
    DOCUMENT,
    DOCUMENT_HASH,
    EDIT,
    EDITS,
    SAVE,
    URL,
    advice,
    lovelace,
    refused,
    stored,
    use_backend,
)

STRATEGY = {"strategy": {"type": "original-states"}}


@pytest.mark.parametrize(
    "arguments",
    [
        {"patch": [], "python_transform": "config['title'] = 'New'"},
        {"patch": []},
    ],
    ids=["two_modes", "patch_without_hash"],
)
async def test_an_incomplete_request_is_refused_before_anything_is_sent(
    core_client: HomeAssistantClient, arguments: dict[str, Any]
) -> None:
    with sent_messages(core_client) as spy:
        body = await refused(core_client, **arguments)

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["action"] == "patch"
    spy.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        {"title": "Renamed"},
        {"show_in_sidebar": False},
        {"url_path": " "},
        {"return_screenshot": True, "view_path": " "},
    ],
    ids=["title", "false_metadata", "blank_url_path", "blank_view_path"],
)
async def test_a_patch_argument_refused_up_front_keeps_the_patch_unwritten(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    arguments: dict[str, Any],
) -> None:
    """Metadata goes in its own call, even when it is False; a blank path is
    refused before the patch is applied or sent anywhere."""
    client = component_core_client
    await lovelace(hass, client)
    request = {"patch": EDITS["patch"], "config_hash": DOCUMENT_HASH, **arguments}

    with sent_messages(client) as spy, received(hass, EDIT) as seen:
        body = await refused(client, **request)

    assert body["action"] == "patch"
    assert body.get("write_committed", False) is False
    assert await stored(client) == DOCUMENT
    spy.assert_not_called()
    assert seen[EDIT] == []


async def test_a_patch_whose_read_got_no_answer_keeps_the_patch_action(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await lovelace(hass, core_client)

    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("offline")),
    ):
        body = await refused(core_client, patch=EDITS["patch"], config_hash="any")

    assert body["action"] == "patch"
    assert body["error"]["code"] == "CONNECTION_FAILED"
    assert await stored(core_client) == DOCUMENT


async def _yaml_dashboard(hass: HomeAssistant) -> None:
    await write_config_files(hass, {"dash.yaml": dump(DOCUMENT)})
    dashboards = {URL: {"mode": "yaml", "filename": "dash.yaml", "title": "Y"}}
    assert await async_setup_component(
        hass, "lovelace", {"lovelace": {"dashboards": dashboards}}
    )


@pytest.mark.parametrize("backend", ["native", "absent"])
@pytest.mark.parametrize("case", ["missing", "strategy", "yaml", "invalid_patch"])
async def test_a_refused_patch_gets_the_same_advice_with_or_without_the_component(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    case: str,
) -> None:
    """A dashboard Core does not have, a strategy dashboard, a YAML dashboard
    (without the component Core refuses its save) and a path the config does
    not have. None is a stale hash, so no fresh hash is suggested."""
    use_backend(hass, monkeypatch, backend)
    config_hash = DOCUMENT_HASH
    if case == "missing":
        assert await async_setup_component(hass, "lovelace", {})
    elif case == "yaml":
        await _yaml_dashboard(hass)
    elif case == "strategy":
        await lovelace(hass, core_client, STRATEGY)
        config_hash = compute_config_hash(STRATEGY)
    else:
        await lovelace(hass, core_client)
    patch_path = {"strategy": "/strategy", "invalid_patch": "/missing-key"}.get(
        case, "/views/0"
    )

    with received(hass, SAVE) as seen:
        body = await refused(
            core_client,
            patch=[{"op": "remove", "path": patch_path}],
            config_hash=config_hash,
        )

    expected = {
        "missing": ("RESOURCE_NOT_FOUND", ("list_only=True", "'config'")),
        "strategy": ("VALIDATION_FAILED", ("Take Control",)),
        "yaml": ("VALIDATION_FAILED", ("YAML", "storage-mode")),
        "invalid_patch": ("VALIDATION_FAILED", ("patch", "JSON Pointer")),
    }[case]
    assert body["error"]["code"] == expected[0]
    assert all(fragment in advice(body) for fragment in expected[1])
    assert "fresh config_hash" not in advice(body)
    assert body["action"] == "patch"
    assert (body["write_committed"], body["post_write_verified"]) == (False, False)
    assert len(seen[SAVE]) == (backend == "absent" and case == "yaml")
    if case in ("strategy", "invalid_patch"):
        assert await stored(core_client) == (
            STRATEGY if case == "strategy" else DOCUMENT
        )


@pytest.mark.parametrize("backend", ["native", "absent"])
@pytest.mark.parametrize("mode", ["config", "patch", "python_transform"])
async def test_a_stale_hash_is_a_conflict_and_only_a_replacement_may_drop_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    mode: str,
) -> None:
    use_backend(hass, monkeypatch, backend)
    await lovelace(hass, core_client)

    with received(hass, SAVE) as seen:
        body = await refused(core_client, config_hash="stale", **{mode: EDITS[mode]})

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert body["reason"] == "conflict"
    assert body["action"] == ("set" if mode == "config" else mode)
    assert (body["write_committed"], body["post_write_verified"]) == (False, False)
    assert "fresh config_hash" in advice(body)
    assert ("omit config_hash" in advice(body)) is (mode == "config")
    assert await stored(core_client) == DOCUMENT
    assert seen[SAVE] == []


async def test_the_component_refuses_to_replace_a_strategy_dashboard(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await lovelace(hass, component_core_client, STRATEGY)

    with received(hass, EDIT, SAVE) as seen:
        body = await refused(component_core_client, config={"views": []})

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert (body["action"], body["write_committed"]) == ("set", False)
    assert "Take Control" in body["error"]["suggestion"]
    assert await stored(component_core_client) == STRATEGY
    assert (len(seen[EDIT]), seen[SAVE]) == (1, [])


@pytest.mark.parametrize(
    ("url_path", "arguments"),
    [
        (
            URL,
            {
                "patch": [
                    {"op": "replace", "path": "/views/0/cards", "value": {"item": []}}
                ],
                "config_hash": DOCUMENT_HASH,
            },
        ),
        ("new-dashboard", {"title": "New", "config": {"views": {"item": []}}}),
    ],
    ids=["patch", "new_dashboard"],
)
async def test_an_item_wrapped_list_is_refused_before_any_write(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    url_path: str,
    arguments: dict[str, Any],
) -> None:
    """A list sent as ``{"item": [...]}`` would be stored as an object: no
    dashboard is created and nothing is saved."""
    await lovelace(hass, core_client)

    with sent_messages(core_client) as spy:
        body = await refused(core_client, url_path, **arguments)

    assert "instead of a JSON array" in body["error"]["message"]
    spy.assert_not_called()
    assert await stored(core_client) == DOCUMENT
