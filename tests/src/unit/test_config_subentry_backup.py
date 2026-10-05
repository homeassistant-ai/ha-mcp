"""Backups of config subentry edits and deletions (#2632).

The component returns a subentry's data only when a backup asks for it; the
server restores an existing subentry through its reconfigure flow and a
deleted one through its create flow.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.tools import component_api
from ha_mcp.tools.config_entry_backup import helper_backup_id, removal_backup_id

# Importing the component fakes installs the homeassistant.* stubs first.
from .test_component_ws_search import FakeConfigEntry, FakeHass, FakeSubentry, wsapi

PLANE = {"declination": 30, "azimuth": 180, "modules_power": 1200}
_FORM = {
    "type": "form",
    "flow_id": "f1",
    "data_schema": [{"name": key} for key in PLANE],
}


def test_component_returns_subentry_data_only_when_a_backup_asks() -> None:
    entry = FakeConfigEntry(
        "ollama",
        subentries={
            "s1": FakeSubentry("s1", "conversation", "Agent", data={"key": "S3CRET"})
        },
    )
    hass = FakeHass(config_entries=[entry])
    secrets = frozenset({"S3CRET"})

    listed = wsapi._do_config_entries(hass, {"domain": "ollama"}, secret_values=secrets)
    asked = wsapi._do_config_entries(
        hass,
        {"domain": "ollama", "include_subentry_data": True},
        secret_values=secrets,
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


class _HA:
    """Subentries of entry ``e1`` as Home Assistant holds them."""

    def __init__(self, subentries: dict[str, dict[str, Any]]) -> None:
        self.subentries = subentries
        self.client = AsyncMock()
        self.client.start_config_subentry_flow.return_value = dict(_FORM)
        self.client.submit_config_subentry_flow_step.side_effect = self._submit
        self.client.list_config_subentries.side_effect = self._list

    parent_exists = True
    scrub_degraded = False

    async def ws(self, client: Any, message: dict[str, Any]) -> dict[str, Any]:
        assert message["include_subentry_data"] is True
        rows = [
            {"subentry_id": sid, "subentry_type": "plane", "data": dict(data)}
            for sid, data in self.subentries.items()
        ]
        entries = [{"entry_id": "e1", "subentries": rows}] if self.parent_exists else []
        reply: dict[str, Any] = {"entries": entries}
        if self.scrub_degraded:
            reply["secret_scrub_degraded"] = True
        return reply

    async def _list(self, entry_id: str) -> dict[str, Any]:
        return {
            "success": True,
            "result": [{"subentry_id": sid} for sid in self.subentries],
        }

    async def _submit(self, flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        kwargs = self.client.start_config_subentry_flow.await_args.kwargs
        if kwargs["subentry_id"] is None:
            self.subentries["s2"] = payload
            return {"type": "create_entry"}
        self.subentries[kwargs["subentry_id"]] = payload
        return {"type": "abort", "reason": "reconfigure_successful"}


@pytest.fixture
def component(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = SimpleNamespace(capabilities={"config_entries_subentry_data"})
    monkeypatch.setattr(
        component_api, "get_component_caps", AsyncMock(return_value=caps)
    )


def _snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": "e1",
        "subentry_id": "s1",
        "subentry_type": "plane",
        "data": data,
    }


@pytest.mark.usefixtures("component")
async def test_subentry_capture_saves_its_data(monkeypatch: pytest.MonkeyPatch) -> None:
    ha = _HA({"s1": dict(PLANE)})
    monkeypatch.setattr(bm, "_ws_send", ha.ws)

    captured = await bm._fetch_config_subentry(ha.client, "e1/s1")

    assert captured["data"] == PLANE
    assert captured["subentry_type"] == "plane"


@pytest.mark.parametrize(
    ("caps", "reason"),
    [
        (
            SimpleNamespace(capabilities={"config_entries"}),
            "config_subentry_read_unsupported",
        ),
        (None, "component_unavailable"),  # not installed, or did not answer
    ],
)
async def test_capture_refuses_a_component_that_cannot_read_subentry_data(
    monkeypatch: pytest.MonkeyPatch, caps: Any, reason: str
) -> None:
    monkeypatch.setattr(
        component_api, "get_component_caps", AsyncMock(return_value=caps)
    )

    with pytest.raises(bm._FlowHelperReadError) as refused:
        await bm._fetch_config_subentry(AsyncMock(), "e1/s1")

    assert refused.value.reason == reason


@pytest.mark.usefixtures("component")
async def test_capture_refuses_redacted_data_and_a_degraded_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither a placeholder nor an unscrubbed secret may become a snapshot."""
    ha = _HA({"s1": {**PLANE, "api_key": "**redacted**"}})
    monkeypatch.setattr(bm, "_ws_send", ha.ws)
    with pytest.raises(bm._FlowHelperReadError) as redacted:
        await bm._fetch_config_subentry(ha.client, "e1/s1")
    assert redacted.value.reason == "redacted_data"

    ha = _HA({"s1": dict(PLANE)})
    ha.scrub_degraded = True
    monkeypatch.setattr(bm, "_ws_send", ha.ws)
    with pytest.raises(bm._FlowHelperReadError) as degraded:
        await bm._fetch_config_subentry(ha.client, "e1/s1")
    assert degraded.value.reason == "secret_scrub_degraded"


@pytest.mark.usefixtures("component")
async def test_missing_parent_entry_is_named_not_treated_as_a_deleted_subentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ha = _HA({})
    ha.parent_exists = False
    monkeypatch.setattr(bm, "_ws_send", ha.ws)

    with pytest.raises(bm.BackupRestoreError, match="e1 no longer exists") as refused:
        await bm._restore_config_subentry(ha.client, "e1/s1", _snapshot(dict(PLANE)))

    assert refused.value.outcome["reason"] == "parent_entry_missing"
    assert refused.value.outcome["apply_status"] == "not_applied"
    ha.client.start_config_subentry_flow.assert_not_awaited()


@pytest.mark.usefixtures("component")
async def test_edited_subentry_is_restored_through_its_reconfigure_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ha = _HA({"s1": {**PLANE, "modules_power": 900}})
    monkeypatch.setattr(bm, "_ws_send", ha.ws)

    await bm._restore_config_subentry(ha.client, "e1/s1", _snapshot(dict(PLANE)))

    assert ha.subentries["s1"] == PLANE
    ha.client.abort_config_subentry_flow.assert_not_awaited()


@pytest.mark.usefixtures("component")
async def test_field_no_form_offers_must_still_hold_its_stored_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {**PLANE, "model": "A"}
    ha = _HA({"s1": dict(stored)})
    ha.client.start_config_subentry_flow.return_value = {**_FORM, "last_step": True}
    monkeypatch.setattr(bm, "_ws_send", ha.ws)

    with pytest.raises(bm.BackupRestoreError) as refused:
        await bm._restore_config_subentry(
            ha.client, "e1/s1", _snapshot({**PLANE, "model": "B"})
        )

    assert refused.value.outcome["apply_status"] == "not_applied"
    assert refused.value.outcome["reason"] == "identity_changed"
    assert ha.subentries["s1"] == stored
    ha.client.abort_config_subentry_flow.assert_awaited_once_with("f1")


@pytest.mark.usefixtures("component")
async def test_deleted_subentry_is_created_again_from_its_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ha = _HA({})
    monkeypatch.setattr(bm, "_ws_send", ha.ws)

    result = await bm._restore_config_subentry(
        ha.client, "e1/s1", _snapshot(dict(PLANE))
    )

    assert ha.subentries == {"s2": PLANE}
    assert result["entity_id"] == "e1/s2"
    assert result["original_subentry_id"] == "s1"


@pytest.mark.usefixtures("component")
async def test_create_flow_that_aborts_is_reported_as_aborted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forecast.Solar aborts a second plane without an API key; that is an
    abort, not a missing form."""
    ha = _HA({"s2": {**PLANE, "azimuth": 90}})  # a different plane exists
    ha.client.start_config_subentry_flow.return_value = {
        "type": "abort",
        "flow_id": "f1",
        "reason": "api_key_required",
    }
    monkeypatch.setattr(bm, "_ws_send", ha.ws)

    with pytest.raises(bm.BackupRestoreError) as refused:
        await bm._restore_config_subentry(ha.client, "e1/s1", _snapshot(dict(PLANE)))

    assert refused.value.outcome["reason"] == "flow_aborted"
    assert refused.value.outcome["apply_status"] == "not_applied"
    ha.client.submit_config_subentry_flow_step.assert_not_awaited()


@pytest.mark.usefixtures("component")
async def test_restoring_a_deleted_subentry_twice_creates_no_second_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recreated subentry has a new id, so the snapshot's id stays absent;
    a repeat restore must find the copy instead of adding another."""
    ha = _HA({})
    monkeypatch.setattr(bm, "_ws_send", ha.ws)
    await bm._restore_config_subentry(ha.client, "e1/s1", _snapshot(dict(PLANE)))

    with pytest.raises(bm.BackupRestoreError, match="e1/s2 already holds") as again:
        await bm._restore_config_subentry(ha.client, "e1/s1", _snapshot(dict(PLANE)))

    assert again.value.outcome["reason"] == "already_recreated"
    assert again.value.outcome["entity_id"] == "e1/s2"
    assert ha.subentries == {"s2": PLANE}
    ha.client.start_config_subentry_flow.assert_awaited_once()


@pytest.mark.usefixtures("component")
async def test_unchanged_field_no_form_offers_does_not_fail_the_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ha = _HA({"s1": {**PLANE, "modules_power": 900, "model": "A"}})

    async def submit(flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        ha.subentries["s1"] = {**payload, "model": "A"}  # kept by the integration
        return {"type": "abort", "reason": "reconfigure_successful"}

    ha.client.submit_config_subentry_flow_step.side_effect = submit
    monkeypatch.setattr(bm, "_ws_send", ha.ws)

    await bm._restore_config_subentry(
        ha.client, "e1/s1", _snapshot({**PLANE, "model": "A"})
    )

    assert ha.subentries["s1"] == {**PLANE, "model": "A"}
