"""The script config tools on a real ``hass``.

The tools run on the server's own client against Core's script config views,
which keep scripts in scripts.yaml in the test's own config directory. A
"renamed" script keeps its storage key (the registry unique_id) while its
entity_id changes in the entity registry, as a rename in the UI leaves it.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import ATTR_DOMAIN, ATTR_SERVICE, EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import dump

from ha_mcp.backup_manager import BackupManager
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import entity_registration, tools_config_scripts
from ha_mcp.tools.entity_registration import post_write_entity
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools
from ha_mcp.tools.ws_waiters import wait_for_entity_registered

from ._blueprint_actions_core import BEDTIME, SCRIPT_PATH
from ._command_faults import before_handling, registry_lookup_failing, unanswered
from ._config_dir_yaml import write_config_files
from ._scripts_core import (
    BODY,
    KEY,
    NEW_SEQUENCE,
    TRANSFORM,
    WRITE_MODES,
    stored_scripts,
    write_arguments,
)
from ._scripts_core import tools as tools
from ._tool_errors import rejected_call_body


async def _rename(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, object_id: str
) -> str:
    """Rename ``script.storage_key`` in the registry; return the new entity_id."""
    entry = entity_registry.async_update_entity(
        f"script.{KEY}", new_entity_id=f"script.{object_id}"
    )
    await hass.async_block_till_done()
    return entry.entity_id


def _waits() -> Any:
    """Spy on the post-write state wait; it still waits on Core."""
    return patch.object(
        tools_config_scripts,
        "wait_for_entity_registered",
        wraps=wait_for_entity_registered,
    )


def _resolutions() -> Any:
    """Spy on the post-write registry resolution; it still reads Core."""
    return patch.object(
        tools_config_scripts,
        "post_write_entity",
        wraps=post_write_entity,
    )


@pytest.mark.parametrize("mode", WRITE_MODES)
async def test_wait_follows_a_renamed_script_to_its_current_entity(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mode: str,
) -> None:
    """Waiting on ``script.<storage_key>`` after a rename never succeeds and
    reports a saved script as not queryable."""
    renamed = await _rename(hass, entity_registry, "current_name")
    arguments = await write_arguments(tools, mode, KEY)

    with _waits() as waited:
        result = await tools.ha_config_set_script(script_id=KEY, **arguments)

    assert result["success"] is True
    waited.assert_awaited_once_with(core_client, renamed)
    assert result["entity_id"] == renamed
    assert not any("not yet queryable" in w for w in result.get("warnings", []))


async def test_a_write_by_a_renamed_scripts_entity_id_updates_it(
    hass: HomeAssistant, tools: ConfigScriptTools, entity_registry: er.EntityRegistry
) -> None:
    """The entity id leads to the stored key, so the reply says updated."""
    renamed = await _rename(hass, entity_registry, "current_name")

    result = await tools.ha_config_set_script(
        script_id=renamed, config={"alias": "Morning", "sequence": NEW_SEQUENCE}
    )

    assert result["operation"] == "updated"
    assert (await stored_scripts(hass))[KEY]["sequence"] == NEW_SEQUENCE


@pytest.mark.parametrize("wait", [True, False])
@pytest.mark.parametrize("mode", WRITE_MODES)
async def test_bulk_write_without_wait_skips_resolution_and_wait(
    tools: ConfigScriptTools, mode: str, wait: bool
) -> None:
    """``wait=False`` without a category is for bulk writes: no registry
    round-trips or state wait slow each one down."""
    arguments = await write_arguments(tools, mode, KEY)

    with _resolutions() as resolved, _waits() as waited:
        result = await tools.ha_config_set_script(script_id=KEY, wait=wait, **arguments)

    assert result["success"] is True
    assert resolved.called is wait
    assert waited.called is wait


@pytest.mark.parametrize("mode", WRITE_MODES)
async def test_registry_outage_waits_on_the_callers_entity(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mode: str,
) -> None:
    """With no registry answer after the write, the wait falls back to the
    entity the caller named, not to ``script.<storage_key>``, which a rename
    left behind."""
    renamed = await _rename(hass, entity_registry, "caller_alias")
    arguments = await write_arguments(tools, mode, "caller_alias")

    with (
        patch.object(
            entity_registration,
            "fetch_entity_lookup_via_component",
            side_effect=HomeAssistantConnectionError("offline"),
        ),
        _waits() as waited,
    ):
        result = await tools.ha_config_set_script(script_id=renamed, **arguments)

    assert result["success"] is True
    waited.assert_awaited_once_with(core_client, renamed)


async def test_unqueryable_script_is_saved_with_a_warning(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled script never gets a state: the write still succeeds and
    says the script is not queryable yet."""
    entity_registry.async_update_entity(
        f"script.{KEY}", disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    monkeypatch.setattr(
        tools_config_scripts,
        "wait_for_entity_registered",
        partial(wait_for_entity_registered, timeout=0),
    )

    result = await tools.ha_config_set_script(
        script_id=KEY, config={"alias": "Morning", "sequence": NEW_SEQUENCE}
    )

    assert result["success"] is True
    assert any("not yet queryable" in w for w in result["warnings"])
    assert (await stored_scripts(hass))[KEY]["sequence"] == NEW_SEQUENCE


async def test_connection_lost_during_wait_keeps_the_write_successful(
    hass: HomeAssistant, tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    with patch.object(
        core_client,
        "get_entity_state",
        side_effect=HomeAssistantConnectionError("offline"),
    ):
        result = await tools.ha_config_set_script(
            script_id=KEY, config={"alias": "Morning", "sequence": NEW_SEQUENCE}
        )

    assert result["success"] is True
    assert any("verification failed" in w for w in result["warnings"])
    assert (await stored_scripts(hass))[KEY]["sequence"] == NEW_SEQUENCE


async def test_new_script_with_a_sequence_is_saved(
    hass: HomeAssistant, tools: ConfigScriptTools
) -> None:
    config = {"alias": "Test Script", "sequence": NEW_SEQUENCE}

    result = await tools.ha_config_set_script(script_id="test_script", config=config)

    assert result["success"] is True
    assert (await stored_scripts(hass))["test_script"] == config


@pytest.mark.parametrize(
    ("extra", "stored_sequence"),
    [
        ({}, None),
        ({"sequence": []}, None),
        ({"sequence": NEW_SEQUENCE}, NEW_SEQUENCE),
    ],
    ids=["no_sequence", "empty_sequence", "own_sequence"],
)
async def test_blueprint_script_keeps_the_blueprints_sequence(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    extra: dict[str, Any],
    stored_sequence: list[dict[str, Any]] | None,
) -> None:
    """An empty ``sequence`` beside ``use_blueprint`` replaces the blueprint's
    sequence with nothing (#466); only a sequence the caller wrote is saved."""
    await write_config_files(hass, {f"blueprints/script/{SCRIPT_PATH}": BEDTIME})
    use_blueprint = {"path": SCRIPT_PATH, "input": {"light": "light.kitchen"}}

    result = await tools.ha_config_set_script(
        script_id="bedtime",
        config={"alias": "Bedtime", "use_blueprint": use_blueprint, **extra},
    )

    assert result["success"] is True
    stored = (await stored_scripts(hass))["bedtime"]
    assert stored["use_blueprint"] == use_blueprint
    assert stored.get("sequence") == stored_sequence


async def test_unparseable_config_string_is_refused(
    tools: ConfigScriptTools,
) -> None:
    arguments: dict[str, Any] = {"config": '{"invalid": json}'}

    body = await rejected_call_body(
        tools.ha_config_set_script(script_id=KEY, **arguments)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_JSON"
    assert "Invalid config parameter" in body["error"]["message"]


async def test_config_that_is_not_an_object_is_refused(
    tools: ConfigScriptTools,
) -> None:
    arguments: dict[str, Any] = {"config": '["sequence"]'}

    body = await rejected_call_body(
        tools.ha_config_set_script(script_id=KEY, **arguments)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "must be a JSON object" in body["error"]["message"]


async def test_get_of_a_renamed_script_returns_its_storage_key(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    entity_registry: er.EntityRegistry,
) -> None:
    """The returned ``script_id`` is the key the set and remove tools write
    to (#1334), not the entity name the caller looked it up by."""
    await _rename(hass, entity_registry, "morning_routine")

    result = await tools.ha_config_get_script(script_id="morning_routine")

    assert result["action"] == "get"
    assert result["script_id"] == KEY
    assert result["config"] == BODY


async def test_get_returns_the_script_body_not_the_rest_envelope(
    tools: ConfigScriptTools,
) -> None:
    """``result["config"]["sequence"]`` must exist without unwrapping a second
    ``config`` (#2329)."""
    result = await tools.ha_config_get_script(script_id=KEY)

    assert result["config"] == BODY
    assert result["config_hash"]


async def test_get_puts_the_category_into_the_body(
    tools: ConfigScriptTools,
    entity_registry: er.EntityRegistry,
    category_registry: cr.CategoryRegistry,
) -> None:
    category_id = category_registry.async_create(
        name="Lighting", scope="script"
    ).category_id
    entity_registry.async_update_entity(
        f"script.{KEY}", categories={"script": category_id}
    )

    result = await tools.ha_config_get_script(script_id=KEY)

    assert result["config"] == {**BODY, "category": category_id}


@pytest.mark.parametrize(
    ("mode", "with_hash"),
    [("full_config", False), ("full_config", True), ("transform", True)],
    ids=["full_config", "full_config_with_hash", "transform"],
)
async def test_write_to_a_renamed_script_updates_its_storage_key(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mode: str,
    with_hash: bool,
) -> None:
    """A write by the renamed entity's id updates the script rather than
    creating a second one under that name. The tool resolved the key and
    hands it over instead of looking it up again (#1813)."""
    renamed = await _rename(hass, entity_registry, "renamed_script")
    arguments = await write_arguments(tools, mode, renamed)
    if with_hash and mode == "full_config":
        current = await tools.ha_config_get_script(script_id=renamed)
        arguments["config_hash"] = current["config_hash"]

    with patch.object(
        core_client, "upsert_script_config", wraps=core_client.upsert_script_config
    ) as upserts:
        result = await tools.ha_config_set_script(
            script_id=renamed, wait=False, **arguments
        )

    assert result["success"] is True
    stored = await stored_scripts(hass)
    assert list(stored) == [KEY]
    assert stored[KEY] != BODY
    args, kwargs = upserts.call_args
    assert args[1] == renamed
    assert kwargs["resolved_id"] == KEY


async def test_a_transform_reads_its_hash_back_without_the_registry(
    hass: HomeAssistant, tools: ConfigScriptTools
) -> None:
    """Once Core has the transformed script, its registry answers only errors:
    the write that landed still succeeds, with the stored config's hash."""
    current = await tools.ha_config_get_script(script_id=f"script.{KEY}")
    saved: list[Event] = []

    @callback
    def reloads_scripts(data: Mapping[str, Any]) -> bool:
        return bool(data[ATTR_DOMAIN] == "script" and data[ATTR_SERVICE] == "reload")

    @callback
    def note(event: Event) -> None:
        # In the loop, not in the executor: seen before the readback's lookup.
        saved.append(event)

    hass.bus.async_listen(EVENT_CALL_SERVICE, note, reloads_scripts)

    def fail_once_saved(msg: dict[str, Any]) -> None:
        if saved:
            raise HomeAssistantError("registry unavailable")

    with before_handling(hass, "config/entity_registry/get", fail_once_saved):
        result = await tools.ha_config_set_script(
            script_id=f"script.{KEY}",
            python_transform=TRANSFORM,
            config_hash=current["config_hash"],
            wait=False,
        )

    assert saved
    assert result["success"] is True
    stored = await tools.ha_config_get_script(script_id=KEY)
    assert stored["config"]["mode"] == "single"
    assert result["config_hash"] == stored["config_hash"]


async def test_a_transform_returns_the_hash_of_the_config_core_stored(
    tools: ConfigScriptTools,
) -> None:
    """Core stores a ``service:`` step as ``action:``: the transform returns the
    hash of what Core stored, which the next hash-checked write must match."""
    current = await tools.ha_config_get_script(script_id=KEY)

    result = await tools.ha_config_set_script(
        script_id=KEY,
        python_transform=(
            "config['sequence'].append({'service': 'homeassistant.update_entity',"
            " 'target': {'entity_id': 'script.storage_key'}})"
        ),
        config_hash=current["config_hash"],
        wait=False,
        MandatoryBPS=False,
    )

    stored = await tools.ha_config_get_script(script_id=KEY)
    assert "action" in stored["config"]["sequence"][-1]
    assert result["config_hash"] == stored["config_hash"]


async def test_write_without_alias_keeps_the_callers_name(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    entity_registry: er.EntityRegistry,
) -> None:
    """A renamed script written without an ``alias`` is named after the
    caller's identifier, not reset to its storage key (#1935)."""
    renamed = await _rename(hass, entity_registry, "renamed_script")
    current = await tools.ha_config_get_script(script_id=renamed)

    await tools.ha_config_set_script(
        script_id=renamed,
        config={"sequence": NEW_SEQUENCE},
        config_hash=current["config_hash"],
        wait=False,
    )

    assert (await stored_scripts(hass))[KEY] == {
        "alias": "renamed_script",
        "sequence": NEW_SEQUENCE,
    }


@pytest.mark.parametrize("source", ["parameter", "config", "transform"])
async def test_unknown_category_is_refused_before_anything_is_written(
    hass: HomeAssistant, tools: ConfigScriptTools, source: str
) -> None:
    """Core accepts any category id on the entity, so a typo would leave the
    script saved with a dangling category (#2159)."""
    config = {"alias": "Morning", "sequence": NEW_SEQUENCE}
    arguments: dict[str, Any] = {"category": "ghost_category"}
    if source == "config":
        arguments = {"config": {**config, "category": "ghost_category"}}
    elif source == "parameter":
        arguments["config"] = config
    else:
        arguments.update(await write_arguments(tools, "transform", KEY))

    body = await rejected_call_body(
        tools.ha_config_set_script(script_id=KEY, wait=False, **arguments)
    )

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["category"] == "ghost_category"
    assert body["scope"] == "script"
    assert await stored_scripts(hass) == {KEY: BODY}


async def test_unreachable_category_registry_refuses_the_write(
    hass: HomeAssistant, tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    """A category that cannot be checked is not written unchecked."""
    with (
        patch.object(
            core_client,
            "send_websocket_message",
            side_effect=HomeAssistantConnectionError("offline"),
        ),
        patch.object(
            core_client, "upsert_script_config", wraps=core_client.upsert_script_config
        ) as upserts,
    ):
        body = await rejected_call_body(
            tools.ha_config_set_script(
                script_id=KEY,
                config={"alias": "Morning", "sequence": NEW_SEQUENCE},
                category="lighting",
                wait=False,
            )
        )

    assert body["error"]["code"] == "CONNECTION_FAILED"
    upserts.assert_not_called()
    assert await stored_scripts(hass) == {KEY: BODY}


@pytest.mark.parametrize("mode", WRITE_MODES)
async def test_category_lands_on_a_renamed_scripts_current_entity(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    entity_registry: er.EntityRegistry,
    category_registry: cr.CategoryRegistry,
    mode: str,
) -> None:
    """After a rename ``script.<storage_key>`` names no entity; the category
    goes to the entity the storage key is registered under, also without the
    component."""
    category_id = category_registry.async_create(
        name="Lighting", scope="script"
    ).category_id
    renamed = await _rename(hass, entity_registry, "renamed_alias")
    arguments = await write_arguments(tools, mode, KEY)

    result = await tools.ha_config_set_script(
        script_id=KEY, category=category_id, wait=False, **arguments
    )

    assert result["category"] == category_id
    entry = entity_registry.async_get(renamed)
    assert entry is not None
    assert entry.categories == {"script": category_id}


class TestCollidingIds:
    """``key_a`` is renamed to ``script.first``, then ``key_b`` to ``script.key_a``:
    the bare key ``key_a`` names A, the entity id ``script.key_a`` names B, and
    ``script.key_b``, which no entity has, names B by its key (run and the
    category guard that a ``script.<x>`` id is not taken as its entity)."""

    @pytest.fixture(autouse=True)
    async def _collide(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        entity_registry: er.EntityRegistry,
    ) -> None:
        scripts = {
            "key_a": {"alias": "A", "sequence": [{"event": "ran_a"}]},
            "key_b": {"alias": "B", "sequence": [{"event": "ran_b"}]},
        }
        await write_config_files(hass, {"scripts.yaml": dump(scripts)})
        await hass.services.async_call("script", "reload", blocking=True)
        entity_registry.async_update_entity(
            "script.key_a", new_entity_id="script.first"
        )
        entity_registry.async_update_entity(
            "script.key_b", new_entity_id="script.key_a"
        )
        await hass.async_block_till_done()

    async def test_get_by_entity_id_reads_that_entitys_script(
        self, tools: ConfigScriptTools
    ) -> None:
        got = await tools.ha_config_get_script(script_id="script.key_a")

        assert (got["script_id"], got["config"]["alias"]) == ("key_b", "B")

    @pytest.mark.parametrize(
        ("script_id", "aliases", "kept"),
        [
            ("key_a", {"key_a": "New", "key_b": "B"}, "key_b"),
            ("script.key_a", {"key_a": "A", "key_b": "New"}, "key_a"),
        ],
    )
    async def test_set_and_remove_reach_the_named_script(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        script_id: str,
        aliases: dict[str, str],
        kept: str,
    ) -> None:
        await tools.ha_config_set_script(
            script_id=script_id, config={**BODY, "alias": "New"}, wait=False
        )
        stored = await stored_scripts(hass)
        assert {key: body["alias"] for key, body in stored.items()} == aliases

        removed = await tools.ha_config_remove_script(script_id=script_id)

        assert set(await stored_scripts(hass)) == {kept}
        # The removal wait watched the deleted script's entity, not the other one.
        assert "warnings" not in removed

    @pytest.mark.parametrize(
        ("script_id", "remove"),
        [
            ("script.key_a", True),
            ("script.key_a", False),
            ("script.first", False),
            ("first", False),
        ],
    )
    async def test_an_entity_id_the_registry_cannot_look_up_writes_nothing(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        script_id: str,
        remove: bool,
    ) -> None:
        """A failed lookup is no answer: ``script.key_a`` is not key ``key_a``,
        and neither ``script.first`` nor ``first``, key_a's entity slug, creates
        key ``first``."""

        call = (
            tools.ha_config_remove_script(script_id=script_id, wait=False)
            if remove
            else tools.ha_config_set_script(
                script_id=script_id, config={**BODY, "alias": "New"}, wait=False
            )
        )
        with registry_lookup_failing(hass):
            body = await rejected_call_body(call)

        assert "registry unavailable" in body["error"]["message"]
        stored = await stored_scripts(hass)
        assert {key: s["alias"] for key, s in stored.items()} == {
            "key_a": "A",
            "key_b": "B",
        }

    async def test_a_repeated_remove_by_key_leaves_the_other_script(
        self, hass: HomeAssistant, tools: ConfigScriptTools
    ) -> None:
        """Once key_a is gone, script.key_a is still key_b's entity."""
        await tools.ha_config_remove_script(script_id="key_a", wait=False)

        body = await rejected_call_body(
            tools.ha_config_remove_script(script_id="key_a")
        )

        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any("'script.key_a'" in s for s in body["error"]["suggestions"])
        assert set(await stored_scripts(hass)) == {"key_b"}

    @pytest.mark.parametrize("mode", ["config", "hashed", "transform"])
    async def test_a_recreate_by_a_removed_key_leaves_the_other_script(
        self, hass: HomeAssistant, tools: ConfigScriptTools, mode: str
    ) -> None:
        """Once key_a is gone, a set by key_a must not overwrite key_b, whose
        entity is script.key_a."""
        await tools.ha_config_remove_script(script_id="key_a", wait=False)
        edit: dict[str, Any] = (
            {"python_transform": TRANSFORM}
            if mode == "transform"
            else {"config": {**BODY, "alias": "A again"}}
        )
        if mode != "config":
            got = await tools.ha_config_get_script(script_id="key_a")
            edit["config_hash"] = got["config_hash"]

        body = await rejected_call_body(
            tools.ha_config_set_script(script_id="key_a", wait=False, **edit)
        )

        assert body["error"]["code"] == "VALIDATION_FAILED"
        assert any("'script.key_a'" in s for s in body["error"]["suggestions"])
        assert {k: s["alias"] for k, s in (await stored_scripts(hass)).items()} == {
            "key_b": "B"
        }

    async def test_restoring_a_removed_key_leaves_the_other_script(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        backup_manager: BackupManager,
    ) -> None:
        """A snapshot taken by the bare key key_a restores key_a, not key_b,
        whose entity is script.key_a."""
        path = await backup_manager.maybe_snapshot("script", "key_a", force=True)
        assert path is not None
        await tools.ha_config_remove_script(script_id="key_a", wait=False)

        await backup_manager.restore_snapshot(path.name)

        assert {k: s["alias"] for k, s in (await stored_scripts(hass)).items()} == {
            "key_a": "A",
            "key_b": "B",
        }

    async def test_a_snapshot_by_entity_id_restores_that_entitys_script(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        backup_manager: BackupManager,
    ) -> None:
        """A snapshot taken as script.key_a holds key_b and restores it there."""
        path = await backup_manager.maybe_snapshot("script", "script.key_a", force=True)
        assert path is not None
        await tools.ha_config_set_script(
            script_id="script.key_a",
            config={"alias": "Changed", "sequence": [{"event": "ran_b"}]},
            wait=False,
        )

        await backup_manager.restore_snapshot(path.name)

        assert {k: s["alias"] for k, s in (await stored_scripts(hass)).items()} == {
            "key_a": "A",
            "key_b": "B",
        }

    async def test_a_removed_key_has_no_snapshot(
        self, tools: ConfigScriptTools, backup_manager: BackupManager
    ) -> None:
        """A snapshot by the bare key key_a never holds key_b, whose entity is
        script.key_a."""
        await tools.ha_config_remove_script(script_id="key_a", wait=False)

        assert (
            await backup_manager.maybe_snapshot("script", "key_a", force=True) is None
        )

    @pytest.mark.parametrize(
        ("script_id", "event"),
        [("key_a", "ran_a"), ("script.key_a", "ran_b"), ("script.key_b", "ran_b")],
    )
    async def test_run_starts_the_named_script(
        self, hass: HomeAssistant, tools: ConfigScriptTools, script_id: str, event: str
    ) -> None:
        fired: list[str] = []
        for name in ("ran_a", "ran_b"):
            hass.bus.async_listen(name, lambda e: fired.append(str(e.event_type)))

        await tools.ha_config_set_script(script_id=script_id, run="start")
        await hass.async_block_till_done()

        assert fired == [event]

    async def test_a_key_the_registry_cannot_look_up_runs_nothing(
        self, hass: HomeAssistant, tools: ConfigScriptTools
    ) -> None:
        """A failed lookup is no answer: script.key_a, the entity id key_a
        would have without its rename, is B."""
        fired: list[str] = []
        for name in ("ran_a", "ran_b"):
            hass.bus.async_listen(name, lambda e: fired.append(str(e.event_type)))

        with registry_lookup_failing(hass, "config/entity_registry/list"):
            body = await rejected_call_body(
                tools.ha_config_set_script(script_id="key_a", run="start")
            )
        await hass.async_block_till_done()

        assert "registry unavailable" in body["error"]["message"]
        assert "Nothing was started" in " ".join(body["error"]["suggestions"])
        assert fired == []

    async def test_an_unanswered_lookup_runs_nothing(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fired: list[str] = []
        for name in ("ran_a", "ran_b"):
            hass.bus.async_listen(name, lambda e: fired.append(str(e.event_type)))

        async with unanswered(hass, "config/entity_registry/list", monkeypatch):
            body = await rejected_call_body(
                tools.ha_config_set_script(script_id="key_a", run="start")
            )
        await hass.async_block_till_done()

        assert "Nothing was started" in " ".join(body["error"]["suggestions"])
        assert fired == []

    async def test_a_key_the_registry_cannot_look_up_is_removed_unverified(
        self, hass: HomeAssistant, tools: ConfigScriptTools
    ) -> None:
        """The delete goes by the key; only the wait needs its entity."""
        with registry_lookup_failing(hass, "config/entity_registry/list"):
            result = await tools.ha_config_remove_script(script_id="key_a")

        assert set(await stored_scripts(hass)) == {"key_b"}
        assert any("registry unavailable" in w for w in result["warnings"])

    async def test_a_key_without_a_registry_entry_has_no_entity(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        entity_registry: er.EntityRegistry,
        category_registry: cr.CategoryRegistry,
    ) -> None:
        """Core registers every loaded script under its key, so no entry is no
        entity: script.key_a is B's, which neither runs nor lends its category."""
        category_id = category_registry.async_create(
            name="Lighting", scope="script"
        ).category_id
        entity_registry.async_update_entity(
            "script.key_a", categories={"script": category_id}
        )
        entity_registry.async_remove("script.first")
        await hass.async_block_till_done()
        fired: list[str] = []
        for name in ("ran_a", "ran_b"):
            hass.bus.async_listen(name, lambda e: fired.append(str(e.event_type)))

        body = await rejected_call_body(
            tools.ha_config_set_script(script_id="key_a", run="start")
        )
        got = await tools.ha_config_get_script(script_id="key_a")
        await hass.async_block_till_done()

        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert fired == []
        assert got["config"]["alias"] == "A"
        assert "category" not in got["config"]

    @pytest.mark.parametrize("mode", WRITE_MODES)
    async def test_a_write_the_registry_cannot_look_up_lends_no_category(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        entity_registry: er.EntityRegistry,
        category_registry: cr.CategoryRegistry,
        mode: str,
    ) -> None:
        """After the write a failed lookup is no answer: script.key_a is B's."""
        category_id = category_registry.async_create(
            name="Lighting", scope="script"
        ).category_id
        arguments = await write_arguments(tools, mode, "key_a")

        with registry_lookup_failing(hass, "config/entity_registry/list"):
            result = await tools.ha_config_set_script(
                script_id="key_a", category=category_id, **arguments
            )

        assert result["success"] is True
        entry = entity_registry.async_get("script.key_a")
        assert entry is not None and entry.categories == {}
        # The skip warning alone: no step ran on another object's entity.
        (warning,) = result["warnings"]
        assert "registry unavailable" in warning
        assert "wait" in warning and "category" in warning

    @pytest.mark.parametrize(
        ("script_id", "entity_id"),
        [
            ("key_a", "script.first"),
            ("script.key_a", "script.key_a"),
            ("script.key_b", "script.key_a"),
        ],
    )
    async def test_get_reports_the_category_of_the_scripts_renamed_entity(
        self,
        tools: ConfigScriptTools,
        entity_registry: er.EntityRegistry,
        category_registry: cr.CategoryRegistry,
        script_id: str,
        entity_id: str,
    ) -> None:
        category_id = category_registry.async_create(
            name="Lighting", scope="script"
        ).category_id
        entity_registry.async_update_entity(
            entity_id, categories={"script": category_id}
        )

        got = await tools.ha_config_get_script(script_id=script_id)

        assert got["config"]["category"] == category_id

    async def test_a_get_by_a_key_the_registry_cannot_look_up_reads_no_category(
        self,
        hass: HomeAssistant,
        tools: ConfigScriptTools,
        entity_registry: er.EntityRegistry,
        category_registry: cr.CategoryRegistry,
    ) -> None:
        """The config still reads, and B's category, on script.key_a, is not
        reported for A."""
        category_id = category_registry.async_create(
            name="Lighting", scope="script"
        ).category_id
        entity_registry.async_update_entity(
            "script.key_a", categories={"script": category_id}
        )

        with registry_lookup_failing(hass, "config/entity_registry/list"):
            got = await tools.ha_config_get_script(script_id="key_a")

        assert got["config"]["alias"] == "A"
        assert "category" not in got["config"]
        # The other warning: no component, so no references.
        assert [w for w in got["warnings"] if "registry unavailable" in w] == [
            got["warnings"][0]
        ]
        assert len(got["warnings"]) == 2

    async def test_the_id_search_reports_opens_that_script(
        self, tools: ConfigScriptTools, core_client: HomeAssistantClient
    ) -> None:
        result = await SmartSearchTools(client=core_client).deep_search(
            query="B", search_types=["script"], limit=10, exact_match=True
        )
        (match,) = [m for m in result["scripts"] if m["entity_id"] == "script.key_a"]
        assert match["script_id"] == "key_b"

        got = await tools.ha_config_get_script(script_id=match["script_id"])

        assert got["config"]["alias"] == "B"
