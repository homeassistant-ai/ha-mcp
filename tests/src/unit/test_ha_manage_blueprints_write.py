"""``ha_manage_blueprints`` ``save`` and ``import`` on real Core (#2329).

Core's ``blueprint/save`` and ``blueprint/import`` write and read blueprint
files in the test's config directory; ``import`` downloads through Core's own
importer, with the URLs it fetches served by ``aioclient_mock``. The tool maps
Core's failures onto its error codes, applies the ``min_version`` gate ``save``
skips, and captures a snapshot before a write replaces an installed file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_blueprints import BlueprintTools

from ._blueprint_actions_core import (
    BARE_YAML,
    BEDTIME,
    BEDTIME_V2,
    MOTION,
    MOTION_V2,
    PATH,
    SCRIPT_PATH,
    enable_auto_backup,
    frames,
    installed_text,
    set_up_core,
    snapshot_texts,
)
from ._tool_errors import rejected_call_body

_URL = "https://example.com/motion.yaml"
_URL_PATH = "example.com/motion.yaml"
_SCRIPT_URL = "https://example.com/bedtime.yaml"
_SCRIPT_URL_PATH = "example.com/bedtime.yaml"
_FUTURE = BARE_YAML + "  homeassistant:\n    min_version: 9999.1.0\n"
_PAST = BARE_YAML + "  homeassistant:\n    min_version: 2020.1.0\n"


async def _save(client: HomeAssistantClient, **params: Any) -> dict[str, Any]:
    result: dict[str, Any] = await BlueprintTools(client).ha_manage_blueprints(
        action="save", **params
    )
    return result


async def _import(client: HomeAssistantClient, url: str = _URL, **params: Any) -> Any:
    return await BlueprintTools(client).ha_manage_blueprints(
        action="import", url=url, **params
    )


async def _listed_source_url(
    client: HomeAssistantClient, path: str, domain: str = "automation"
) -> Any:
    """The ``source_url`` Core lists for the blueprint at ``path``."""
    listing = await client.send_websocket_message(
        {"type": "blueprint/list", "domain": domain}
    )
    return listing["result"][path]["metadata"].get("source_url")


# ------------------------------------------------------------------------ save


async def test_save_requires_yaml(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    body = await rejected_call_body(_save(core_client, path=PATH))

    assert body["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
    assert "'yaml' is required for action='save'" in body["error"]["message"]
    sent.assert_not_called()


async def test_save_new_path_sends_neither_source_url_nor_override(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """A hand-authored blueprint carries no source_url: the key must be absent
    (Core's ``vol.Optional``), not ``None``, and allow_override is not sent."""
    await set_up_core(hass)

    result = await _save(core_client, path=PATH, yaml=MOTION)

    assert result == {
        "success": True,
        "data": {
            "domain": "automation",
            "path": PATH,
            "overrides_existing": False,
            "message": "Blueprint saved.",
        },
    }
    assert frames(sent, "blueprint/save") == [
        {"type": "blueprint/save", "domain": "automation", "path": PATH, "yaml": MOTION}
    ]
    assert await installed_text(hass, "automation", PATH) is not None
    assert await _listed_source_url(core_client, PATH) is None


async def test_save_overwrite_stamps_source_url_and_reports_the_reload(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    await set_up_core(hass, {f"script/{SCRIPT_PATH}": BEDTIME})

    result = await _save(
        core_client,
        domain="script",
        path=SCRIPT_PATH,
        yaml=BEDTIME_V2,
        overwrite=True,
        source_url=_SCRIPT_URL,
    )

    assert result["data"]["overrides_existing"] is True
    assert "reloaded" in result["data"]["message"]
    assert frames(sent, "blueprint/save") == [
        {
            "type": "blueprint/save",
            "domain": "script",
            "path": SCRIPT_PATH,
            "yaml": BEDTIME_V2,
            "source_url": _SCRIPT_URL,
            "allow_override": True,
        }
    ]
    assert await _listed_source_url(core_client, SCRIPT_PATH, "script") == _SCRIPT_URL
    assert "Bedtime v2" in str(await installed_text(hass, "script", SCRIPT_PATH))


async def test_save_appends_the_extension_core_would(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """Core writes ``<path>.yaml`` when the path lacks the suffix, so the tool
    sends and reports the path Core actually writes, not one that will never
    exist in the store."""
    await set_up_core(hass)

    result = await _save(core_client, path="user/motion", yaml=MOTION)

    assert result["data"]["path"] == PATH
    assert frames(sent, "blueprint/save")[0]["path"] == PATH
    assert await installed_text(hass, "automation", PATH) is not None


async def test_save_existing_without_overwrite_is_already_exists(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass, {f"automation/{PATH}": MOTION})

    body = await rejected_call_body(_save(core_client, path=PATH, yaml=MOTION_V2))

    error = body["error"]
    assert error["code"] == "RESOURCE_ALREADY_EXISTS"
    assert "File already exists" in error["message"]
    assert "overwrite=True" in " ".join(error["suggestions"])
    assert await installed_text(hass, "automation", PATH) == MOTION


async def test_save_invalid_blueprint_is_validation_failed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass)

    body = await rejected_call_body(
        _save(core_client, path=PATH, yaml="blueprint:\n  name: Motion Light\n")
    )

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert await installed_text(hass, "automation", PATH) is None


async def test_save_invalid_yaml_classifies_by_cores_error_code(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The client flattens Core's error to a string and carries its structured
    code in a sibling ``error_code`` key: that key, not the message text, must
    drive the mapping (a YAML parse error mentions no "invalid")."""
    await set_up_core(hass)

    body = await rejected_call_body(
        _save(core_client, path=PATH, yaml="not: [a blueprint")
    )

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert "while parsing a flow sequence" in body["error"]["message"]


async def test_save_core_cannot_write_is_service_call_failed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A path whose parent is a file: Core cannot write it, which is no fault of the YAML."""
    await set_up_core(hass, {"automation/user": "not a directory\n"})

    body = await rejected_call_body(_save(core_client, path=PATH, yaml=MOTION))

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"


async def test_save_refuses_a_blueprint_this_home_assistant_cannot_run(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """blueprint/save skips the min_version gate import applies, so the tool
    applies it itself, with Core's wording."""
    await set_up_core(hass)

    body = await rejected_call_body(_save(core_client, path=PATH, yaml=_FUTURE))

    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert "Requires at least Home Assistant 9999.1.0" in body["error"]["message"]
    assert frames(sent, "blueprint/save") == []
    assert await installed_text(hass, "automation", PATH) is None


async def test_save_proceeds_when_min_version_is_satisfied(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    await set_up_core(hass)
    with patch.object(core_client, "get_config", wraps=core_client.get_config) as asked:
        result = await _save(core_client, path=PATH, yaml=_PAST)

    assert result["success"] is True
    asked.assert_awaited_once()
    assert len(frames(sent, "blueprint/save")) == 1


async def test_save_without_min_version_never_asks_for_the_version(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up_core(hass)
    with patch.object(core_client, "get_config", wraps=core_client.get_config) as asked:
        result = await _save(core_client, path=PATH, yaml=MOTION)

    assert result["success"] is True
    asked.assert_not_awaited()


async def test_save_proceeds_when_the_version_lookup_fails(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The min_version check exists to refuse a blueprint this Home Assistant
    cannot run; a transport failure while asking for the version must not block
    a save Core itself would accept."""
    await set_up_core(hass)
    with patch.object(
        core_client,
        "get_config",
        AsyncMock(side_effect=HomeAssistantConnectionError("socket gone")),
    ):
        result = await _save(core_client, path=PATH, yaml=_FUTURE)

    assert result["success"] is True
    assert await installed_text(hass, "automation", PATH) is not None


async def test_overwriting_save_snapshots_the_path_core_writes(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``save`` with ``overwrite=True`` on ``user/motion`` replaces the installed
    ``user/motion.yaml``; the pre-write capture has to look there, not at a
    path that does not exist on any tier (PR #2356)."""
    await set_up_core(hass, {f"automation/{PATH}": MOTION})
    manager = enable_auto_backup(monkeypatch, tmp_path, component_core_client)

    result = await _save(
        component_core_client, path="user/motion", yaml=MOTION_V2, overwrite=True
    )

    assert result["data"]["path"] == PATH
    assert snapshot_texts(manager, "blueprint_automation", PATH) == [MOTION]
    assert "Motion Light v2" in str(await installed_text(hass, "automation", PATH))


# ---------------------------------------------------------------------- import


async def test_import_saves_and_reports_the_installed_path(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    sent: AsyncMock,
) -> None:
    aioclient_mock.get(_URL, text=MOTION)
    await set_up_core(hass)

    result = await _import(core_client)

    assert result["success"] is True
    assert result["data"]["imported_blueprint"] == {
        "path": _URL_PATH,
        "domain": "automation",
        "name": "Motion Light",
        "description": "Turn on a light on motion.",
    }
    assert result["data"]["overrides_existing"] is False
    [save] = frames(sent, "blueprint/save")
    assert (save["path"], save["source_url"]) == (_URL_PATH, _URL)
    # allow_override is only sent when actually overwriting (older HA schemas
    # reject the unknown key).
    assert "allow_override" not in save
    assert await installed_text(hass, "automation", _URL_PATH) is not None
    assert await _listed_source_url(core_client, _URL_PATH) == _URL


async def test_import_ignores_the_domain_parameter(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The blueprint file declares its own domain, so a stray value must not
    refuse an otherwise valid import."""
    aioclient_mock.get(_URL, text=MOTION)
    await set_up_core(hass)

    result = await _import(core_client, domain="light")

    assert result["success"] is True
    assert await installed_text(hass, "automation", _URL_PATH) is not None


async def test_import_that_validates_but_cannot_be_saved_is_a_service_failure(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The write can still fail after import validated the blueprint.

    The path was free, so the early already-exists gate let this through and
    the failure came from the write itself: a different situation from a
    rejected ``save`` action, with its own error mapping.
    """
    aioclient_mock.get(_URL, text=MOTION)
    await set_up_core(hass, {"automation/example.com": "not a directory\n"})

    body = await rejected_call_body(_import(core_client))

    error = body["error"]
    assert error["code"] == "SERVICE_CALL_FAILED", error
    assert "validated but could not be saved" in " ".join(error["suggestions"])


async def test_a_save_race_on_import_is_reported_as_already_exists(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Core reports exists=false for an installed file that failed to load, so
    the save's own already-exists answer is the authoritative one."""
    aioclient_mock.get(_URL, text=MOTION)
    await set_up_core(hass, {f"automation/{_URL_PATH}": "blueprint: [unclosed\n"})

    body = await rejected_call_body(_import(core_client))

    assert body["error"]["code"] == "RESOURCE_ALREADY_EXISTS", body["error"]
    assert "overwrite=true" in " ".join(body["error"]["suggestions"])


async def test_import_existing_without_overwrite_is_rejected(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    sent: AsyncMock,
) -> None:
    aioclient_mock.get(_URL, text=MOTION_V2)
    await set_up_core(hass, {f"automation/{_URL_PATH}": MOTION})

    body = await rejected_call_body(_import(core_client))

    assert body["error"]["code"] == "RESOURCE_ALREADY_EXISTS"
    assert frames(sent, "blueprint/save") == []
    assert await installed_text(hass, "automation", _URL_PATH) == MOTION


async def test_import_with_overwrite_replaces_the_installed_blueprint(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    sent: AsyncMock,
) -> None:
    aioclient_mock.get(_URL, text=MOTION_V2)
    await set_up_core(hass, {f"automation/{_URL_PATH}": MOTION})

    result = await _import(core_client, overwrite=True)

    assert result["data"]["overrides_existing"] is True
    assert frames(sent, "blueprint/save")[0]["allow_override"] is True
    assert "Motion Light v2" in str(await installed_text(hass, "automation", _URL_PATH))


async def test_import_failure_keeps_the_network_suggestions(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Importing is the only action that makes HA reach the internet, so the
    connectivity remedies belong on this path."""
    aioclient_mock.get(_URL, status=404)
    await set_up_core(hass)

    body = await rejected_call_body(_import(core_client))

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    joined = " ".join(body["error"]["suggestions"])
    assert "internet access" in joined
    assert "different source" in joined


async def test_import_rejects_non_http_url(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    body = await rejected_call_body(_import(core_client, "ftp://example.com/bp.yaml"))

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    sent.assert_not_called()


async def test_overwriting_import_snapshots_the_installed_blueprint(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A re-import over an installed file is a write the decorator cannot see
    (the path is only known after blueprint/import), so it captures inline,
    under the blueprint's own domain."""
    aioclient_mock.get(_SCRIPT_URL, text=BEDTIME_V2)
    await set_up_core(hass, {f"script/{_SCRIPT_URL_PATH}": BEDTIME})
    manager = enable_auto_backup(monkeypatch, tmp_path, component_core_client)

    await _import(component_core_client, _SCRIPT_URL, overwrite=True)

    assert snapshot_texts(manager, "blueprint_script", _SCRIPT_URL_PATH) == [BEDTIME]


async def test_every_overwriting_import_attempts_the_capture(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Core reports ``exists=false`` for an installed file that failed to load,
    and ``allow_override`` replaces that file just the same: the capture is
    attempted whenever overwrite is set, so the broken file is kept."""
    broken = "blueprint: [unclosed\n"
    aioclient_mock.get(_URL, text=MOTION)
    await set_up_core(hass, {f"automation/{_URL_PATH}": broken})
    manager = enable_auto_backup(monkeypatch, tmp_path, component_core_client)

    await _import(component_core_client, overwrite=True)

    assert snapshot_texts(manager, "blueprint_automation", _URL_PATH) == [broken]
    assert "Motion Light" in str(await installed_text(hass, "automation", _URL_PATH))


async def test_import_without_overwrite_captures_nothing(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A plain import cannot replace anything (Core refuses the save without
    ``allow_override``), so there is nothing to snapshot."""
    aioclient_mock.get(_URL, text=MOTION)
    await set_up_core(hass)
    manager = enable_auto_backup(monkeypatch, tmp_path, component_core_client)

    await _import(component_core_client)

    assert manager.list_snapshots() == []
    assert await installed_text(hass, "automation", _URL_PATH) is not None


async def test_import_normalises_a_yml_suggestion_the_way_core_does(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sent: AsyncMock,
) -> None:
    """Core's importer only strips ``.yaml`` from a GitHub filename and its save
    only recognises ``.yaml``, so a ``foo.yml`` suggestion is written as
    ``foo.yml.yaml``. The tool saves, snapshots and reports that path."""
    yml_path = "user/motion.yml.yaml"
    aioclient_mock.get(
        "https://raw.githubusercontent.com/user/repo/main/motion.yml", text=MOTION_V2
    )
    await set_up_core(hass, {f"automation/{yml_path}": MOTION})
    manager = enable_auto_backup(monkeypatch, tmp_path, component_core_client)

    result = await _import(
        component_core_client,
        "https://github.com/user/repo/blob/main/motion.yml",
        overwrite=True,
    )

    assert frames(sent, "blueprint/save")[0]["path"] == yml_path
    assert result["data"]["imported_blueprint"]["path"] == yml_path
    assert snapshot_texts(manager, "blueprint_automation", yml_path) == [MOTION]
    assert "Motion Light v2" in str(await installed_text(hass, "automation", yml_path))
