"""Integration auto-backups: the snapshot carries the entry's options when the
component can read them safely, and a restore puts them back through the
options flow before it reapplies the enabled state. A deleted entry is refused.

The server's client runs on a real ``hass``: Core holds the entries of a test
integration whose options flow puts back what it is given, answers every
command, and the ``ha_mcp_tools`` component's own commands answer where a test
has the component. ``received`` lists, in order, the component reads and the
enable/disable commands Core received and the options submitted to the flow.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import probatio
import pytest
from homeassistant.components import websocket_api
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryDataWithId,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from custom_components.ha_mcp_tools.websocket_api.constants import WS_CONFIG_ENTRIES
from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_integrations import fetch_integration, restore_integration
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import Settings
from ha_mcp.redaction import REDACTED_SET

from ._backup_core import backup_settings as backup_settings
from ._backup_core import manage_backup
from ._component_frames import dropped, pooled

_DOMAIN = "backup_planes"
_DISABLE = "config_entries/disable"

# The options form: ``int`` and ``flag``; any other option stays as stored.
_FORM = probatio.Schema(
    {probatio.Optional("int"): int, probatio.Optional("flag"): bool}
)


@dataclass
class _Planes:
    """What the test integration saw; a ``held`` submit waits for ``release``."""

    log: list[dict[str, Any]] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class _PlanesOptionsFlow(OptionsFlow):
    """Stores the submitted options over the entry's, unless its ``scenario``
    (in the entry's data) says otherwise."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        scenario = self.config_entry.data.get("scenario")
        if scenario == "refuses":
            return self.async_abort(reason="not_now")
        if user_input is None:
            return self.async_show_form(
                step_id="init", data_schema=_FORM, last_step=True
            )
        planes: _Planes = self.hass.data[_DOMAIN]
        planes.log.append({"type": "options_flow", "options": user_input})
        if scenario == "held":
            planes.entered.set()
            await planes.release.wait()
        if scenario == "fails":
            raise RuntimeError("the plane did not answer")
        if scenario == "keeps_its_own":
            return self.async_create_entry(data=dict(self.config_entry.options))
        if scenario == "secrets_unreadable":
            secrets = Path(self.hass.config.path("secrets.yaml"))
            await self.hass.async_add_executor_job(secrets.write_text, "plane: [\n")
        return self.async_create_entry(data={**self.config_entry.options, **user_input})


class _PlanesConfigFlow(ConfigFlow):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _PlanesOptionsFlow()


@pytest.fixture
async def planes(hass: HomeAssistant) -> AsyncIterator[_Planes]:
    """An integration whose entries load and unload, its options flow, and
    Core's entry commands."""

    async def loaded(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        return True

    hass.data[_DOMAIN] = seen = _Planes()
    mock_integration(
        hass, MockModule(_DOMAIN, async_setup_entry=loaded, async_unload_entry=loaded)
    )
    mock_platform(hass, f"{_DOMAIN}.config_flow", None)
    with mock_config_flow(_DOMAIN, _PlanesConfigFlow):
        assert await async_setup_component(hass, "config", {})
        yield seen


@pytest.fixture
def received(
    hass: HomeAssistant, planes: _Planes, component_core_client: HomeAssistantClient
) -> list[dict[str, Any]]:
    """What Core received, in order: component reads, enable/disable commands
    and options-flow submits. Core's own handlers answer each command."""
    log = planes.log
    for command in (WS_CONFIG_ENTRIES, _DISABLE):
        core_handler, schema = hass.data[websocket_api.DOMAIN][command]

        def record(
            core: HomeAssistant,
            connection: websocket_api.ActiveConnection,
            msg: dict[str, Any],
            core_handler: Any = core_handler,
        ) -> None:
            log.append({k: v for k, v in msg.items() if k != "id"})
            core_handler(core, connection, msg)

        websocket_api.async_register_command(hass, command, record, schema)
    return log


def _types(log: list[dict[str, Any]]) -> list[str]:
    return [message["type"] for message in log]


def _entry(
    hass: HomeAssistant,
    disabled_by: ConfigEntryDisabler | None = None,
    *,
    options: dict[str, Any] | None = None,
    scenario: str | None = None,
    **kwargs: Any,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=_DOMAIN,
        options={"int": 11} if options is None else options,
        data={"scenario": scenario},
        disabled_by=disabled_by,
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


async def _restore(
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    disabled_by: str | None = None,
    **snapshot: Any,
) -> Any:
    return await restore_integration(
        client,
        entry.entry_id,
        {"domain": _DOMAIN, "disabled_by": disabled_by, **snapshot},
    )


async def _secrets(hass: HomeAssistant, text: str) -> None:
    path = Path(hass.config.path("secrets.yaml"))
    await hass.async_add_executor_job(path.write_text, text)


async def test_restoring_a_deleted_entry_is_refused_and_changes_nothing(
    hass: HomeAssistant, core_client: HomeAssistantClient, planes: _Planes
) -> None:
    other = _entry(hass, ConfigEntryDisabler.USER)
    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await restore_integration(
            core_client, "e1", {"domain": "otp", "disabled_by": None}
        )
    assert exc_info.value.outcome["reason"] == "entry_deleted"
    assert exc_info.value.outcome["apply_status"] == "not_applied"
    assert "Config entry e1 (otp) no longer exists" in str(exc_info.value)
    assert hass.config_entries.async_entries() == [other]
    assert other.disabled_by is ConfigEntryDisabler.USER


@pytest.mark.parametrize(
    ("snapshot", "current", "expected"),
    [
        ("user", None, ConfigEntryDisabler.USER),
        (None, ConfigEntryDisabler.USER, None),
    ],
    ids=["disables", "enables"],
)
async def test_a_snapshot_taken_before_options_were_kept_reports_them_not_restored(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    planes: _Planes,
    snapshot: str | None,
    current: ConfigEntryDisabler | None,
    expected: ConfigEntryDisabler | None,
) -> None:
    """The enabled state is still put back; the options are reported."""
    entry = _entry(hass, current)
    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await restore_integration(
            core_client, entry.entry_id, {"domain": _DOMAIN, "disabled_by": snapshot}
        )
    assert exc_info.value.outcome["reason"] == "options_not_restored"
    assert exc_info.value.outcome["apply_status"] == "applied"
    assert exc_info.value.outcome["verification_status"] == "unavailable"
    assert "taken before options were captured" in str(exc_info.value)
    assert entry.disabled_by is expected


async def _backed_up(client: HomeAssistantClient, entry: MockConfigEntry) -> str:
    """An ``ha_manage_backup`` snapshot of ``entry``; its name."""
    created = await manage_backup(
        client,
        scope="edits",
        action="create",
        domain="integration",
        entity_id=entry.entry_id,
    )
    return str(created["data"]["backup_name"])


async def _refused_restore(client: HomeAssistantClient, name: str) -> dict[str, Any]:
    """The error ``ha_manage_backup`` answers a restore of ``name`` with."""
    with pytest.raises(ToolError) as exc_info:
        await manage_backup(client, scope="edits", action="restore", backup_name=name)
    error: dict[str, Any] = json.loads(str(exc_info.value))["error"]
    return error


def _removed_on_read(hass: HomeAssistant, entry: MockConfigEntry, nth: int) -> None:
    """Core removes ``entry`` as it receives the ``nth`` component read from now,
    then answers that read."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][WS_CONFIG_ENTRIES]
    reads = 0

    async def remove_then_answer(
        core: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        nonlocal reads
        reads += 1
        if reads == nth:
            await core.config_entries.async_remove(entry.entry_id)
        core_handler(core, connection, msg)

    websocket_api.async_register_command(
        hass,
        WS_CONFIG_ENTRIES,
        websocket_api.async_response(remove_then_answer),
        schema,
    )


@pytest.mark.parametrize(
    ("after_its_safety_backup", "expected"),
    [
        (False, ["Add it again"]),
        (True, ["Add it again", "Use safety_backup to inspect or restore"]),
    ],
    ids=["deleted-before-the-restore", "deleted-after-its-safety-backup"],
)
async def test_a_refused_restore_is_not_found_with_the_handlers_guidance(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    planes: _Planes,
    backup_settings: Settings,
    after_its_safety_backup: bool,
    expected: list[str],
) -> None:
    entry = _entry(hass)
    name = await _backed_up(component_core_client, entry)
    if after_its_safety_backup:
        # The safety backup reads the entry first, then the restore does.
        _removed_on_read(hass, entry, 2)
    else:
        await hass.config_entries.async_remove(entry.entry_id)

    error = await _refused_restore(component_core_client, name)

    assert error["code"] == "RESOURCE_NOT_FOUND"
    suggestions = error.get("suggestions") or [error["suggestion"]]
    for suggestion, prefix in zip(suggestions, expected, strict=True):
        assert suggestion.startswith(prefix)
    assert hass.config_entries.async_get_entry(entry.entry_id) is None


async def test_a_refused_restore_without_handler_guidance_gets_the_generic_hint(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    planes: _Planes,
    backup_settings: Settings,
) -> None:
    """The options flow keeps its own options: the restore fails unverified,
    with no guidance of its handler's."""
    entry = _entry(hass, scenario="keeps_its_own")
    name = await _backed_up(component_core_client, entry)
    hass.config_entries.async_update_entry(entry, options={"int": 42})

    error = await _refused_restore(component_core_client, name)

    suggestions = error.get("suggestions") or [error["suggestion"]]
    assert suggestions[0] == (
        "Inspect the current configuration and restore outcome before retrying"
    )
    assert suggestions[1].startswith("Use safety_backup to inspect or restore")
    assert len(suggestions) == 2
    assert entry.options == {"int": 42}


# --- capture ------------------------------------------------------------------


async def test_capture_through_the_component_carries_options_but_not_subentries(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, planes: _Planes
) -> None:
    entry = _entry(
        hass,
        unique_id="plane-1",
        subentries_data=[
            ConfigSubentryDataWithId(
                data={},
                subentry_id="s1",
                subentry_type="wing",
                title="Wing",
                unique_id=None,
            )
        ],
    )

    snapshot = await fetch_integration(component_core_client, entry.entry_id)

    assert snapshot["options"] == dict(entry.options)
    assert snapshot["unique_id"] == entry.unique_id
    assert "subentries" not in snapshot
    assert "options_unavailable" not in snapshot


async def test_capture_without_the_component_keeps_cores_row_and_says_why_no_options(
    hass: HomeAssistant, core_client: HomeAssistantClient, planes: _Planes
) -> None:
    _entry(hass)
    entry = _entry(hass)

    snapshot = await fetch_integration(core_client, entry.entry_id)

    assert snapshot["entry_id"] == entry.entry_id
    assert snapshot["domain"] == _DOMAIN
    assert snapshot["options_unavailable"] == "component_unavailable"
    assert "options" not in snapshot


async def test_capture_whose_component_read_got_no_answer_says_the_read_failed(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, planes: _Planes
) -> None:
    """An installed component that did not answer is not reported missing."""
    _entry(hass)
    entry = _entry(hass)
    socket = await pooled(component_core_client)

    with dropped(hass, WS_CONFIG_ENTRIES, socket):
        snapshot = await fetch_integration(component_core_client, entry.entry_id)

    assert snapshot["entry_id"] == entry.entry_id
    assert snapshot["options_unavailable"] == "component_read_failed"
    assert "options" not in snapshot


async def test_capture_of_a_missing_entry_is_none(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, planes: _Planes
) -> None:
    _entry(hass)

    assert await fetch_integration(component_core_client, "e1") is None


@pytest.mark.parametrize(
    ("secrets", "options", "reason"),
    [
        ("plane: [\n", {"int": 11}, "secret_scrub_degraded"),
        ("plane_token: hunter2\n", {"token": "hunter2"}, "redacted_options"),
        (None, {"nested": {"key": REDACTED_SET}}, "redacted_options"),
    ],
    ids=["secrets-unreadable", "resolved-secret", "nested-placeholder"],
)
async def test_options_that_cannot_be_captured_are_left_out_with_the_reason(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    planes: _Planes,
    secrets: str | None,
    options: dict[str, Any],
    reason: str,
) -> None:
    if secrets is not None:
        await _secrets(hass, secrets)
    entry = _entry(hass, options=options)

    snapshot = await fetch_integration(component_core_client, entry.entry_id)

    assert "options" not in snapshot
    assert snapshot["options_unavailable"] == reason


# --- restore ------------------------------------------------------------------


async def test_restore_puts_back_differing_options_then_the_enabled_state(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
) -> None:
    """``region`` is on no form: it is put back because it is the stored one."""
    entry = _entry(hass, options={"int": 42, "region": "eu"})

    await _restore(component_core_client, entry, options={"int": 11, "region": "eu"})

    assert entry.options == {"int": 11, "region": "eu"}
    assert entry.disabled_by is None
    assert _types(received) == [
        WS_CONFIG_ENTRIES,
        "options_flow",
        WS_CONFIG_ENTRIES,
        _DISABLE,
    ]


@pytest.mark.parametrize("options", [{"int": 11}, {}], ids=["non-empty", "empty"])
async def test_restore_runs_no_options_flow_when_the_options_already_match(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
    options: dict[str, Any],
) -> None:
    entry = _entry(hass, options=options)

    await _restore(component_core_client, entry, "user", options=options)

    assert "options_flow" not in _types(received)
    assert received[-1]["type"] == _DISABLE
    assert entry.disabled_by is ConfigEntryDisabler.USER
    assert entry.options == options


async def test_restore_enables_a_disabled_entry_before_its_options_flow(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
) -> None:
    entry = _entry(hass, ConfigEntryDisabler.USER, options={"int": 42})

    await _restore(component_core_client, entry, options={"int": 11})

    types = _types(received)
    assert types.index(_DISABLE) < types.index("options_flow")
    assert types.count(_DISABLE) == 1
    assert entry.disabled_by is None
    assert entry.options == {"int": 11}


async def test_restore_disables_only_after_the_options_flow(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
) -> None:
    entry = _entry(hass, options={"int": 42})

    await _restore(component_core_client, entry, "user", options={"int": 11})

    assert _types(received) == [
        WS_CONFIG_ENTRIES,
        "options_flow",
        WS_CONFIG_ENTRIES,
        _DISABLE,
    ]
    assert entry.disabled_by is ConfigEntryDisabler.USER
    assert entry.options == {"int": 11}


@pytest.mark.parametrize(
    ("scenario", "stored", "verification"),
    [
        ("keeps_its_own", {"int": 42}, "mismatched"),
        ("secrets_unreadable", {"int": 11}, "unavailable"),
    ],
    ids=["flow-keeps-its-own", "readback-no-longer-scrubbed"],
)
async def test_restore_whose_options_cannot_be_verified_reports_it(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
    scenario: str,
    stored: dict[str, Any],
    verification: str,
) -> None:
    entry = _entry(hass, options={"int": 42}, scenario=scenario)

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(component_core_client, entry, options={"int": 11})

    assert exc_info.value.outcome["apply_status"] == "applied"
    assert exc_info.value.outcome["verification_status"] == verification
    assert entry.options == stored
    assert _DISABLE not in _types(received)


async def test_snapshot_options_are_not_restored_over_options_it_cannot_read(
    hass: HomeAssistant, core_client: HomeAssistantClient, planes: _Planes
) -> None:
    entry = _entry(hass)

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await restore_integration(
            core_client,
            entry.entry_id,
            {"domain": _DOMAIN, "disabled_by": "user", "options": {"int": 42}},
        )

    assert exc_info.value.outcome["reason"] == "options_unreadable"
    assert entry.options == {"int": 11}
    assert entry.disabled_by is None
    assert not hass.config_entries.options.async_progress()


async def test_restoring_a_deleted_entry_read_through_the_component_is_refused(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
) -> None:
    other = _entry(hass, ConfigEntryDisabler.USER)

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await restore_integration(
            component_core_client,
            "e1",
            {"domain": _DOMAIN, "disabled_by": None, "options": {"int": 1}},
        )

    assert exc_info.value.outcome["reason"] == "entry_deleted"
    assert _types(received) == [WS_CONFIG_ENTRIES]
    assert hass.config_entries.async_entries() == [other]
    assert other.disabled_by is ConfigEntryDisabler.USER


async def test_restore_holds_the_entry_write_guard(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    planes: _Planes,
    received: list[dict[str, Any]],
    backup_settings: Settings,
) -> None:
    """Another write of the entry, started while the options flow runs, waits
    until the restore has finished."""
    entry = _entry(hass, scenario="held")
    manager = bm.get_backup_manager(component_core_client, backup_settings)
    taken = await manager.maybe_snapshot("integration", entry.entry_id, force=True)
    assert taken is not None
    hass.config_entries.async_update_entry(entry, options={"int": 42})

    async def write() -> None:
        async with manager.config_entry_write_guard(entry.entry_id):
            received.append({"type": "another write"})

    restore = asyncio.create_task(manager.restore_snapshot(taken.name))
    try:
        async with asyncio.timeout(10):
            await planes.entered.wait()
        other = asyncio.create_task(write())
    finally:
        planes.release.set()
    await asyncio.gather(restore, other)

    assert _types(received)[-4:] == [
        "options_flow",
        WS_CONFIG_ENTRIES,
        _DISABLE,
        "another write",
    ]
    assert entry.options == {"int": 11}


# --- options a restore cannot put back are reported, not passed as success ---


@pytest.mark.parametrize(
    ("snapshot", "current", "disabled_by", "verification", "message"),
    [
        ({"options": {}}, None, "user", "mismatched", "options were not"),
        (
            {"options_unavailable": "redacted_options"},
            ConfigEntryDisabler.USER,
            None,
            "unavailable",
            "redacted_options",
        ),
    ],
    ids=["empty-options", "unreadable-options"],
)
async def test_snapshot_options_that_cannot_be_put_back_are_reported_not_restored(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
    snapshot: dict[str, Any],
    current: ConfigEntryDisabler | None,
    disabled_by: str | None,
    verification: str,
    message: str,
) -> None:
    entry = _entry(hass, current, options={"int": 42})

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(component_core_client, entry, disabled_by, **snapshot)

    assert exc_info.value.outcome["verification_status"] == verification
    assert exc_info.value.outcome["apply_status"] == "applied"
    assert message in str(exc_info.value)
    assert "options_flow" not in _types(received)
    assert received[-1] == {
        "type": _DISABLE,
        "entry_id": entry.entry_id,
        "disabled_by": disabled_by,
    }
    assert entry.options == {"int": 42}
    assert entry.disabled_by is (disabled_by and ConfigEntryDisabler.USER)


@pytest.mark.parametrize(
    ("current", "scenario", "apply_status", "options_status", "reason", "prefix"),
    [
        (
            ConfigEntryDisabler.USER,
            "refuses",
            "applied",
            "not_applied",
            "flow_aborted",
            "was enabled, but",
        ),
        (
            ConfigEntryDisabler.USER,
            "fails",
            "applied",
            "unknown",
            None,
            "was enabled, then its",
        ),
        (None, "refuses", "not_applied", None, "flow_aborted", None),
    ],
    ids=["refused", "unsettled", "enabled-entry-keeps-own-error"],
)
async def test_a_failed_options_flow_says_whether_the_entry_was_enabled_first(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
    current: ConfigEntryDisabler | None,
    scenario: str,
    apply_status: str,
    options_status: str | None,
    reason: str | None,
    prefix: str | None,
) -> None:
    entry = _entry(hass, current, options={"int": 42}, scenario=scenario)

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(component_core_client, entry, options={"int": 11})

    outcome, enabled = exc_info.value.outcome, current is not None
    assert outcome["apply_status"] == apply_status
    assert outcome.get("options_apply_status") == options_status
    assert outcome.get("reason") == reason
    if prefix is not None:
        assert str(exc_info.value).startswith(f"Config entry {entry.entry_id} {prefix}")
    assert ("was enabled" in str(exc_info.value)) is enabled
    assert _types(received).count(_DISABLE) == int(enabled)
    assert entry.disabled_by is None
    assert entry.options == {"int": 42}


async def test_an_integration_restore_takes_a_safety_backup_whatever_the_settings(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    planes: _Planes,
    backup_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)
    entry = _entry(hass, options={"int": 11})
    manager = bm.get_backup_manager(component_core_client, backup_settings)
    taken = await manager.maybe_snapshot("integration", entry.entry_id, force=True)
    assert taken is not None
    hass.config_entries.async_update_entry(entry, options={"int": 42})

    result = await manager.restore_snapshot(taken.name, take_safety_backup=False)

    assert entry.options == {"int": 11}
    safety = manager.read_snapshot(result["safety_backup"])
    assert safety["config"]["options"] == {"int": 42}


@pytest.mark.parametrize("options", [["x"], None], ids=["list", "null"])
async def test_snapshot_options_that_are_not_an_object_are_refused_before_any_write(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
    options: Any,
) -> None:
    entry = _entry(hass, ConfigEntryDisabler.USER, options={"int": 42})

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(component_core_client, entry, options=options)

    assert exc_info.value.outcome["reason"] == "invalid_snapshot"
    assert exc_info.value.outcome["apply_status"] == "not_applied"
    assert _types(received) == [WS_CONFIG_ENTRIES]
    assert entry.disabled_by is ConfigEntryDisabler.USER
    assert entry.options == {"int": 42}


async def test_options_core_finds_equal_are_reported_as_not_matching(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    received: list[dict[str, Any]],
) -> None:
    """Core keeps the options it has when the new ones compare equal
    (``0 == False``), so the snapshot's value cannot be stored: the restore is
    sent and says its result does not match, never that it matched."""
    entry = _entry(hass, options={"flag": 0})

    with pytest.raises(bm.BackupRestoreError) as exc_info:
        await _restore(component_core_client, entry, options={"flag": False})

    assert "options_flow" in _types(received)
    assert exc_info.value.outcome["apply_status"] == "applied"
    assert exc_info.value.outcome["verification_status"] == "mismatched"
    assert type(entry.options["flag"]) is int
