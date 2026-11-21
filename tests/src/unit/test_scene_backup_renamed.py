"""Comparing and restoring a scene backup after its entity was renamed, on real Core.

A scene snapshot is filed under the id it was taken by; the scene it
holds is the one stored under its config's ``id``, which a rename of the
entity does not change. Every stored scene read back is Core's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp.backup_manager import BackupManager, MandatoryBackupError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.backup_on_demand import capture_on_demand

from ._tool_errors import rejected_call_body
from ._tools_config_scenes_core import TV_ON, scene, scene_tools, stored_scenes


@pytest.fixture(params=["renamed", "renamed_and_taken"])
async def snapshot_of_movie(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """A snapshot of ``movie`` taken as ``scene.movie``. Since then its entity
    became ``scene.cinema`` and its stored name "Changed"; with
    ``renamed_and_taken`` the scene ``other`` holds ``scene.movie``."""
    await scene_tools(
        hass,
        core_client,
        monkeypatch,
        [scene("movie", "Movie"), scene("other", "Other")],
    )
    path = await backup_manager.maybe_snapshot("scene", "scene.movie", force=True)
    assert path is not None
    registry = er.async_get(hass)
    registry.async_update_entity("scene.movie", new_entity_id="scene.cinema")
    if request.param == "renamed_and_taken":
        registry.async_update_entity("scene.other", new_entity_id="scene.movie")
    await core_client.upsert_scene_config(
        {"name": "Changed", "entities": dict(TV_ON)}, "movie"
    )
    return path.name


async def test_the_comparison_reads_the_scene_the_snapshot_holds(
    backup_manager: BackupManager, snapshot_of_movie: str
) -> None:
    _snapshot, current = await backup_manager.snapshot_comparison(snapshot_of_movie)

    assert current is not None
    assert (current["id"], current["name"]) == ("movie", "Changed")


async def test_the_restore_writes_the_scene_the_snapshot_holds(
    hass: HomeAssistant, backup_manager: BackupManager, snapshot_of_movie: str
) -> None:
    outcome = await backup_manager.restore_snapshot(snapshot_of_movie)

    names = {s["id"]: s["name"] for s in await stored_scenes(hass)}
    assert names == {"movie": "Movie", "other": "Other"}
    assert outcome["safety_backup"] is not None


@pytest.fixture(params=["deleted", "deleted_and_taken"])
async def snapshot_of_deleted_movie(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """A snapshot of ``movie``, deleted since; with ``deleted_and_taken`` the
    scene ``other`` holds ``scene.movie``, which a bare ``movie`` names once no
    scene is stored under that key."""
    await scene_tools(
        hass,
        core_client,
        monkeypatch,
        [scene("movie", "Movie"), scene("other", "Other")],
    )
    path = await backup_manager.maybe_snapshot("scene", "movie", force=True)
    assert path is not None
    await core_client.delete_scene_config("movie")
    if request.param == "deleted_and_taken":
        er.async_get(hass).async_update_entity(
            "scene.other", new_entity_id="scene.movie"
        )
    return path.name


async def test_the_comparison_finds_a_deleted_scene_gone(
    backup_manager: BackupManager, snapshot_of_deleted_movie: str
) -> None:
    _snapshot, current = await backup_manager.snapshot_comparison(
        snapshot_of_deleted_movie
    )

    assert current is None


async def test_a_deleted_key_has_no_snapshot(
    backup_manager: BackupManager, snapshot_of_deleted_movie: str
) -> None:
    """A snapshot by the bare key movie never holds other, whose entity can be
    scene.movie."""
    reasons: list[str] = []

    path = await backup_manager.maybe_snapshot(
        "scene", "movie", force=True, skip_reasons=reasons
    )

    assert path is None
    assert reasons == []


async def test_an_on_demand_snapshot_by_a_bare_key_names_the_entity_form(
    backup_manager: BackupManager, snapshot_of_deleted_movie: str
) -> None:
    body = await rejected_call_body(capture_on_demand(backup_manager, "scene", "movie"))

    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "'scene.movie'" in body["error"]["suggestions"][0]


async def test_a_deleted_scene_comes_back_under_its_own_key(
    hass: HomeAssistant, backup_manager: BackupManager, snapshot_of_deleted_movie: str
) -> None:
    outcome = await backup_manager.restore_snapshot(snapshot_of_deleted_movie)

    names = {s["id"]: s["name"] for s in await stored_scenes(hass)}
    assert names == {"movie": "Movie", "other": "Other"}
    assert outcome["safety_backup"] is None
    assert outcome["warnings"] == [
        "No safety backup was taken: scene movie no longer exists, "
        "so this restore overwrites none of its state."
    ]


async def test_a_failed_read_of_the_stored_scenes_blocks_the_restore(
    hass: HomeAssistant, backup_manager: BackupManager, snapshot_of_deleted_movie: str
) -> None:
    """Core cannot read an unparsable scenes.yaml, so it cannot say whether
    the scene is gone. The message is the check: a write would fail on the same
    file, so the file's bytes cannot tell a refusal from an attempted write."""
    path = Path(hass.config.path("scenes.yaml"))
    await hass.async_add_executor_job(path.write_text, "- id: [unclosed\n")

    with pytest.raises(MandatoryBackupError, match="stored scenes could not be read"):
        await backup_manager.restore_snapshot(snapshot_of_deleted_movie)
