"""Backups of config subentry edits and deletions (#2632).

The component returns a subentry's data only when a backup asks for it; the
server restores an existing subentry through its reconfigure flow and a
deleted one through its create flow. Another integration's subentry flows run
in a real ``hass`` and answer as its entry's ``form`` says (like Kitchen
Sink's, a named form keeps the name in the subentry title); the server's
client drives them over Core's API, and the backup manager reads the data
through the real component.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import WS_CONFIG_ENTRIES
from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_entry_backup import helper_backup_id, removal_backup_id

from ._component_core import ws_result
from ._component_frames import older_component
from ._core_setup import write_secrets

_DOMAIN = "subentry_planes"
DOMAIN_KEY = "helper_config_subentry"
PLANE = {"declination": 30, "azimuth": 180, "modules_power": 1200}


class PlaneFlow(ConfigSubentryFlow):
    """A plane: its fields, a ``name`` that becomes the title when the entry's
    ``form`` is ``named``, a create that aborts for ``aborts``, a last form
    for ``last_step``, and a stored ``model`` the integration keeps for
    ``keeps_model``."""

    def _form(self) -> str:
        form: str = self._get_entry().data.get("form", "named")
        return form

    def _schema(self) -> probatio.Schema:
        fields: dict[Any, Any] = {probatio.Required(key): int for key in PLANE}
        if self._form() == "named":
            fields[probatio.Optional("name")] = str
        return probatio.Schema(fields)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if self._form() == "aborts":
            return self.async_abort(reason="api_key_required")
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=self._schema())
        data = dict(user_input)
        return self.async_create_entry(
            title=data.pop("name", "Default name"), data=data
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self._schema(),
                last_step=self._form() == "last_step",
            )
        data = dict(user_input)
        title = data.pop("name", subentry.title)
        if self._form() == "keeps_model":
            data["model"] = subentry.data["model"]
        return self.async_update_and_abort(
            self._get_entry(), subentry, data=data, title=title
        )


class PlanesConfigFlow(ConfigFlow):
    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {"plane": PlaneFlow}


@pytest.fixture
async def planes(hass: HomeAssistant) -> AsyncIterator[None]:
    mock_integration(hass, MockModule(_DOMAIN))
    mock_platform(hass, f"{_DOMAIN}.config_flow")
    with mock_config_flow(_DOMAIN, PlanesConfigFlow):
        assert await async_setup_component(hass, "config", {})
        yield


def _entry(
    hass: HomeAssistant, form: str = "named", **subentries: tuple[str, Any]
) -> MockConfigEntry:
    """Entry ``e1`` with ``{subentry_id: (title, data)}`` planes."""
    entry = MockConfigEntry(
        domain=_DOMAIN,
        entry_id="e1",
        data={"form": form},
        subentries_data=[
            {
                "subentry_id": subentry_id,
                "subentry_type": "plane",
                "title": title,
                "data": data,
                "unique_id": None,
            }
            for subentry_id, (title, data) in subentries.items()
        ],
    )
    entry.add_to_hass(hass)
    return entry


def _planes(hass: HomeAssistant) -> dict[str, tuple[str, dict[str, Any]]]:
    """Entry ``e1``'s subentries as Core holds them: ``{id: (title, data)}``."""
    entry = hass.config_entries.async_get_entry("e1")
    assert entry is not None
    return {sid: (sub.title, dict(sub.data)) for sid, sub in entry.subentries.items()}


def _manager(client: HomeAssistantClient, tmp_path: Path, **settings: Any) -> Any:
    return bm.get_backup_manager(
        client,
        SimpleNamespace(
            **{
                "enable_auto_backup": True,
                "auto_backup_throttle_minutes": 0,
                "auto_backup_retain_per_entity": 5,
                "auto_backup_dir": str(tmp_path),
                **settings,
            }
        ),
    )


async def _capture(manager: Any) -> str:
    path = await manager.maybe_snapshot(DOMAIN_KEY, "e1/s1", force=True, mandatory=True)
    assert path is not None
    name: str = path.name
    return name


def _remove_s1(hass: HomeAssistant) -> None:
    entry = hass.config_entries.async_get_entry("e1")
    assert entry is not None
    assert hass.config_entries.async_remove_subentry(entry, "s1")


def _edit_s1(hass: HomeAssistant, title: str, data: dict[str, Any]) -> None:
    entry = hass.config_entries.async_get_entry("e1")
    assert entry is not None
    hass.config_entries.async_update_subentry(
        entry, entry.subentries["s1"], title=title, data=data
    )


def _spy(client: HomeAssistantClient, name: str) -> Any:
    return patch.object(client, name, wraps=getattr(client, name))


# --- the component's read -------------------------------------------------------
async def test_the_component_sends_subentry_data_only_when_a_backup_asks(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    write_secrets(hass, "k: S3CRET\n")
    MockConfigEntry(
        domain="ollama",
        subentries_data=[
            {
                "subentry_id": "s1",
                "subentry_type": "conversation",
                "title": "Agent",
                "data": {"key": "S3CRET"},
                "unique_id": None,
            }
        ],
    ).add_to_hass(hass)

    listed = await ws_result(component_ws, WS_CONFIG_ENTRIES, domain="ollama")
    asked = await ws_result(
        component_ws, WS_CONFIG_ENTRIES, domain="ollama", include_subentry_data=True
    )

    assert "data" not in listed["entries"][0]["subentries"][0]
    assert asked["entries"][0]["subentries"][0]["data"] == {"key": "**redacted**"}


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"entry_id": "e1", "subentry_id": "s1"}, "e1/s1"),
        ({"entry_id": "e1"}, ""),  # a create has nothing to snapshot
    ],
)
def test_subentry_edits_are_keyed_by_entry_and_subentry(
    kwargs: dict[str, Any], expected: str
) -> None:
    assert helper_backup_id({"helper_type": "config_subentry", **kwargs}) == expected


def test_subentry_deletion_is_keyed_by_entry_and_subentry() -> None:
    kwargs = {"helper_type": "config_subentry", "target": "e1", "subentry_id": "s1"}
    assert removal_backup_id(kwargs) == "e1/s1"


# --- capture --------------------------------------------------------------------
@pytest.mark.usefixtures("planes")
async def test_a_subentry_capture_saves_its_data(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    _entry(hass, s1=("Kitchen", PLANE))
    manager = _manager(component_core_client, tmp_path)

    snapshot = manager.read_snapshot(await _capture(manager))

    assert snapshot["config"]["data"] == PLANE
    assert snapshot["config"]["subentry_type"] == "plane"


async def _refusal(manager: Any) -> str:
    with pytest.raises(bm.MandatoryBackupError) as refused:
        await manager.maybe_snapshot(DOMAIN_KEY, "e1/s1", force=True, mandatory=True)
    cause: str = refused.value.safe_cause
    return cause


@pytest.mark.usefixtures("planes")
async def test_a_capture_without_the_component_is_refused(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    _entry(hass, s1=("Kitchen", PLANE))

    cause = await _refusal(_manager(core_client, tmp_path))

    assert "not installed or did not answer" in cause


@pytest.mark.usefixtures("planes")
async def test_a_capture_by_a_component_that_cannot_read_subentries_is_refused(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component older than the subentry-data read."""
    older_component(monkeypatch, "config_entries_subentry_data")
    _entry(hass, s1=("Kitchen", PLANE))

    cause = await _refusal(_manager(component_core_client, tmp_path))

    assert "can read subentry data" in cause


@pytest.mark.usefixtures("planes")
@pytest.mark.parametrize(
    ("secrets", "expected"),
    [
        ("api_key: s3cr3t\n", "redacted values"),
        ("{bad: yaml: [", "scrub is degraded"),
    ],
    ids=["redacted", "degraded"],
)
async def test_redacted_or_unscrubbed_data_never_becomes_a_snapshot(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    secrets: str,
    expected: str,
) -> None:
    """Neither a placeholder nor an unscrubbed secret may become a snapshot."""
    write_secrets(hass, secrets)
    _entry(hass, s1=("Kitchen", {**PLANE, "api_key": "s3cr3t"}))

    cause = await _refusal(_manager(component_core_client, tmp_path / "backups"))

    assert expected in cause


# --- restore ----------------------------------------------------------------------
@pytest.mark.usefixtures("planes")
async def test_an_edited_subentry_is_restored_through_its_reconfigure_flow(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """Its recovery point is taken first, and the snapshot title comes back
    through the named form too."""
    _entry(hass, s1=("Kitchen", PLANE))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    _edit_s1(hass, "Renamed", {**PLANE, "modules_power": 900})

    with _spy(component_core_client, "abort_config_subentry_flow") as abort:
        result = await manager.restore_snapshot(name)

    assert result["safety_backup"] is not None
    assert _planes(hass) == {"s1": ("Kitchen", PLANE)}
    abort.assert_not_awaited()


@pytest.mark.usefixtures("planes")
async def test_a_deleted_subentry_is_created_again_with_its_title(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """Kitchen Sink's entity (and the conversation agents) keep the name in the
    subentry title, never in data; the named form gets it back."""
    _entry(hass, s1=("Kitchen", PLANE))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    _remove_s1(hass)

    result = await manager.restore_snapshot(name)

    ((new_id, restored),) = _planes(hass).items()
    assert restored == ("Kitchen", PLANE)
    assert new_id != "s1"
    recreated = result["result"]
    assert recreated["restore_mode"] == "recreated"
    assert (recreated["entity_id"], recreated["original_subentry_id"]) == (
        f"e1/{new_id}",
        "s1",
    )


@pytest.mark.usefixtures("planes")
async def test_a_recreated_subentry_whose_title_differs_is_reported(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """A form without a name field cannot carry the title back; the readback
    says so instead of reporting a match on data alone."""
    _entry(hass, form="unnamed", s1=("Kitchen", PLANE))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    _remove_s1(hass)

    with pytest.raises(bm.BackupRestoreError, match="data or title") as caught:
        await manager.restore_snapshot(name)

    assert caught.value.outcome["apply_status"] == "applied"
    assert caught.value.outcome["verification_status"] == "mismatched"
    assert [title for title, _ in _planes(hass).values()] == ["Default name"]


@pytest.mark.usefixtures("planes")
async def test_a_sibling_with_the_same_data_but_another_name_is_not_a_recreation(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """Two agents can differ by name alone; the one still present must not
    block restoring the deleted one, and a second restore finds the copy."""
    _entry(hass, s1=("Kitchen", PLANE), s2=("Pantry", PLANE))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    _remove_s1(hass)

    await manager.restore_snapshot(name)
    with pytest.raises(bm.BackupRestoreError) as again:
        await manager.restore_snapshot(name)

    titles = {sid: title for sid, (title, _) in _planes(hass).items()}
    (kitchen,) = [sid for sid, title in titles.items() if title == "Kitchen"]
    assert sorted(titles.values()) == ["Kitchen", "Pantry"]
    assert again.value.outcome["reason"] == "already_recreated"
    assert again.value.outcome["entity_id"] == f"e1/{kitchen}"


@pytest.mark.usefixtures("planes")
async def test_restoring_a_deleted_subentry_twice_creates_no_second_copy(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """The recreated subentry has a new id, so the snapshot's id stays absent;
    a repeat restore must find the copy instead of adding another."""
    _entry(hass, s1=("Kitchen", PLANE))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    _remove_s1(hass)

    with _spy(component_core_client, "start_config_subentry_flow") as start:
        await manager.restore_snapshot(name)
        with pytest.raises(bm.BackupRestoreError, match="already holds") as again:
            await manager.restore_snapshot(name)

    assert again.value.outcome["reason"] == "already_recreated"
    assert len(_planes(hass)) == 1
    start.assert_awaited_once()


@pytest.mark.usefixtures("planes")
async def test_a_missing_parent_entry_is_named_not_treated_as_a_deleted_subentry(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    _entry(hass, s1=("Kitchen", PLANE))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    assert await hass.config_entries.async_remove("e1")

    # The restore's safety capture is the first to find the parent gone.
    with (
        _spy(component_core_client, "start_config_subentry_flow") as start,
        pytest.raises(bm.MandatoryBackupError, match="e1 no longer exists"),
    ):
        await manager.restore_snapshot(name)

    start.assert_not_awaited()


@pytest.mark.usefixtures("planes")
async def test_a_field_no_form_offers_must_still_hold_its_stored_value(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """Home Assistant marks the last form, so the mismatch is caught before
    anything is submitted and the flow is aborted."""
    _entry(hass, form="last_step", s1=("Kitchen", {**PLANE, "model": "B"}))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    stored = {**PLANE, "model": "A"}
    _edit_s1(hass, "Kitchen", stored)

    with (
        _spy(component_core_client, "abort_config_subentry_flow") as abort,
        pytest.raises(bm.BackupRestoreError) as refused,
    ):
        await manager.restore_snapshot(name)

    assert refused.value.outcome["apply_status"] == "not_applied"
    assert refused.value.outcome["reason"] == "identity_changed"
    assert _planes(hass)["s1"][1] == stored
    abort.assert_awaited_once()


@pytest.mark.usefixtures("planes")
async def test_an_unchanged_field_no_form_offers_does_not_fail_the_restore(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    _entry(hass, form="keeps_model", s1=("Kitchen", {**PLANE, "model": "A"}))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    _edit_s1(hass, "Kitchen", {**PLANE, "modules_power": 900, "model": "A"})

    await manager.restore_snapshot(name)

    assert _planes(hass)["s1"][1] == {**PLANE, "model": "A"}


@pytest.mark.usefixtures("planes")
async def test_a_create_flow_that_aborts_is_reported_as_aborted(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """Forecast.Solar aborts a second plane without an API key; that is an
    abort, not a missing form."""
    _entry(hass, form="aborts", s1=("Kitchen", PLANE))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    _remove_s1(hass)

    with (
        _spy(component_core_client, "submit_config_subentry_flow_step") as submit,
        pytest.raises(bm.BackupRestoreError) as refused,
    ):
        await manager.restore_snapshot(name)

    assert refused.value.outcome["reason"] == "flow_aborted"
    assert refused.value.outcome["apply_status"] == "not_applied"
    submit.assert_not_awaited()


@pytest.mark.usefixtures("planes")
async def test_a_restore_takes_its_safety_snapshot_whatever_the_settings(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """A reconfigure flow can apply a restore partly, so the pre-restore
    capture is forced (auto-backup off, throttle) and mandatory; a deleted
    subentry has nothing to capture."""
    _entry(hass, s1=("Kitchen", PLANE))
    manager = _manager(
        component_core_client,
        tmp_path,
        enable_auto_backup=False,
        auto_backup_throttle_minutes=60,
    )
    name = await _capture(manager)
    _edit_s1(hass, "Kitchen", {**PLANE, "modules_power": 900})

    first = await manager.restore_snapshot(name)
    _edit_s1(hass, "Kitchen", {**PLANE, "modules_power": 800})
    second = await manager.restore_snapshot(name)
    _remove_s1(hass)
    third = await manager.restore_snapshot(name)

    assert first["safety_backup"] and second["safety_backup"]
    safety = manager.read_snapshot(second["safety_backup"])
    assert safety["config"]["data"] == {**PLANE, "modules_power": 800}
    assert third["safety_backup"] is None


@pytest.mark.usefixtures("planes")
async def test_a_subentry_that_reappears_after_an_empty_capture_is_not_edited(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    """Absent at the safety capture, the subentry has no recovery point; one
    that exists by the time the restore runs must not be reconfigured."""
    _entry(hass, s1=("Kitchen", PLANE))
    manager = _manager(component_core_client, tmp_path)
    name = await _capture(manager)
    _edit_s1(hass, "Kitchen", {**PLANE, "modules_power": 900})
    handler = manager.handler_for(DOMAIN_KEY)
    assert handler is not None

    async def absent_at_capture(client: Any, entity_id: str) -> None:
        return None

    manager.register(
        bm.DomainHandler(DOMAIN_KEY, fetch=absent_at_capture, restore=handler.restore)
    )

    with (
        _spy(component_core_client, "start_config_subentry_flow") as start,
        pytest.raises(bm.BackupRestoreError) as refused,
    ):
        await manager.restore_snapshot(name)

    assert refused.value.outcome["reason"] == "entry_still_exists"
    assert _planes(hass)["s1"][1]["modules_power"] == 900
    start.assert_not_awaited()
