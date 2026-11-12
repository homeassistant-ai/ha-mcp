"""Settings diff failures preserve missing/invalid/unavailable distinctions.

The Settings backup routes run on the server's own client connected to a real
``hass``, with the auto-backup settings read from the environment. Snapshots
are captures of a real Template helper, or files placed in the backup
directory the way an older release or a corrupted disk left them.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest
import yaml
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import setup_test_component_platform
from starlette.requests import Request
from starlette.responses import Response

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.settings_ui import build_settings_handlers

from ._template_helper_core import create_template_sensor

if TYPE_CHECKING:
    from ha_mcp.server import HomeAssistantSmartMCPServer

NAME = "calendar_event.calendar.fam_evt-1.20260909_000000.yaml"
Route = Callable[[Request], Awaitable[Response]]


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core's config directory is this test's ``tmp_path``."""
    return hass_tmp_config_dir


def _use_backup_dir(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(directory))
    reset_global_settings()


@pytest.fixture
def backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "backups"
    directory.mkdir()
    _use_backup_dir(monkeypatch, directory)
    return directory


def _routes(client: HomeAssistantClient) -> dict[str, Route]:
    """The Settings handlers of a server whose client is ``client``."""
    server = cast("HomeAssistantSmartMCPServer", SimpleNamespace(client=client))
    return build_settings_handlers(server)


async def _call(
    client: HomeAssistantClient, route: str, name: str = NAME
) -> tuple[int, dict[str, Any]]:
    request = Request(
        {"type": "http", "query_string": b"", "path_params": {"name": name}}
    )
    response = await _routes(client)[route](request)
    return response.status_code, json.loads(bytes(response.body))


def _place(directory: Path, name: str, data: dict[str, Any]) -> None:
    payload = {"schema_version": bm.SCHEMA_VERSION, **data}
    (directory / name).write_text(yaml.safe_dump(payload), encoding="utf-8")


def _place_calendar_event(directory: Path) -> None:
    """A snapshot of ``evt-1`` on ``calendar.fam``, fetched again on diff."""
    _place(
        directory,
        NAME,
        {
            "domain": "calendar_event",
            "entity_id": "calendar.fam::evt-1",
            "config": {"calendar_entity_id": "calendar.fam", "uid": "evt-1"},
        },
    )


async def test_settings_inventory_reports_unusable_directory_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("occupied", encoding="utf-8")
    _use_backup_dir(monkeypatch, parent / "backups")

    status, payload = await _call(core_client, "list_backups")

    assert status == 200
    assert payload["enabled"] is False
    manager = bm.get_backup_manager(core_client, get_global_settings())
    assert manager.init_dir_error is not None
    assert parent.read_text(encoding="utf-8") == "occupied"


async def test_settings_diff_of_a_missing_snapshot_is_not_found(
    backup_dir: Path, core_client: HomeAssistantClient
) -> None:
    status, payload = await _call(core_client, "diff_backup")

    assert status == 404
    assert payload["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_settings_diff_of_a_domain_without_handler_is_not_found(
    backup_dir: Path, core_client: HomeAssistantClient
) -> None:
    # A snapshot of a domain this release no longer backs up.
    name = "retired.thing.20260909_000000.yaml"
    _place(backup_dir, name, {"domain": "retired", "entity_id": "thing", "config": {}})

    status, payload = await _call(core_client, "diff_backup", name)

    assert status == 404
    assert payload["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_settings_diff_without_an_answer_from_home_assistant(
    backup_dir: Path, socket_enabled: None
) -> None:
    """Home Assistant does not answer: the reply says so without the error text."""
    _place_calendar_event(backup_dir)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    unreachable = HomeAssistantClient(
        base_url=f"http://127.0.0.1:{port}", token="token", verify_ssl=False
    )
    try:
        with pytest.raises(HomeAssistantConnectionError) as refused:
            await unreachable.get_config()
        status, payload = await _call(unreachable, "diff_backup")
    finally:
        await unreachable.close()

    assert status == 502
    assert payload["error"]["code"] == "CONNECTION_FAILED"
    assert str(refused.value) not in json.dumps(payload)


class UnreadableCalendar(CalendarEntity):
    """``calendar.fam``: its backend fails every read with a private detail."""

    _attr_name = "Fam"

    @property
    def event(self) -> CalendarEvent | None:
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: dt.datetime, end_date: dt.datetime
    ) -> list[CalendarEvent]:
        raise HomeAssistantError("option-value-secret upstream body")


async def test_settings_diff_logs_safe_failure_diagnostics(
    hass: HomeAssistant,
    backup_dir: Path,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup_test_component_platform(hass, "calendar", [UnreadableCalendar()])
    assert await async_setup_component(
        hass, "calendar", {"calendar": {"platform": "test"}}
    )
    await hass.async_block_till_done()
    _place_calendar_event(backup_dir)

    with caplog.at_level(logging.WARNING):
        status, _payload = await _call(core_client, "diff_backup")

    assert status == 502
    warnings = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "comparison" in warnings.lower()
    assert NAME in warnings
    assert "HomeAssistantAPIError" in warnings
    assert "500" in warnings
    assert "option-value-secret" not in warnings


@pytest.fixture
async def template_snapshot(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    backup_dir: Path,
) -> Path:
    """A capture of a Template sensor helper made by Core's config flow."""
    entry = await create_template_sensor(hass, "saved")
    manager = bm.get_backup_manager(component_core_client, get_global_settings())
    path = await manager.maybe_snapshot("helper_template", entry.entry_id, force=True)
    assert path is not None
    return path


async def test_settings_diff_preserves_template_refusal_reason(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    template_snapshot: Path,
) -> None:
    # A present but unreadable secrets.yaml: options would go out unscrubbed.
    secrets = Path(hass.config.path("secrets.yaml"))
    await hass.async_add_executor_job(secrets.write_text, "key: [broken\n")

    status, payload = await _call(
        component_core_client, "diff_backup", template_snapshot.name
    )

    assert status == 409
    assert payload["error"]["code"] == "CONFIG_VALIDATION_FAILED"
    assert payload["error"]["message"] == (
        "Template helper secret scrub is degraded; capture is unsafe"
    )
    assert payload["data"]["reason"] == "secret_scrub_degraded"


@pytest.mark.parametrize("action", ["view", "diff", "restore"])
async def test_settings_snapshot_validation_never_echoes_yaml_values(
    backup_dir: Path, core_client: HomeAssistantClient, action: str
) -> None:
    (backup_dir / NAME).write_text(
        "config:\n  private-config-value: [broken\n", encoding="utf-8"
    )

    status, payload = await _call(core_client, f"{action}_backup")

    assert status == 400
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert payload["error"]["message"] == (
        "Snapshot is invalid; inspect its YAML and schema version"
    )
    assert "private-config-value" not in json.dumps(payload)


ENTRY = object()  # stands for the captured helper's entry id


@pytest.mark.parametrize(
    "config",
    [
        {"options": {"name": "Example", "template_type": "sensor", "state": "1"}},
        {"entry_id": ENTRY},
        {"entry_id": ENTRY, "options": []},
    ],
    ids=["missing-identity", "missing-options", "invalid-options"],
)
async def test_settings_diff_rejects_invalid_template_snapshot_before_fetch(
    component_core_client: HomeAssistantClient,
    template_snapshot: Path,
    config: dict[str, Any],
) -> None:
    manager = bm.get_backup_manager(component_core_client, get_global_settings())
    data = manager.read_snapshot(template_snapshot.name)
    entry_id = data["config"]["entry_id"]
    data["config"] = {k: entry_id if v is ENTRY else v for k, v in config.items()}
    _place(template_snapshot.parent, template_snapshot.name, data)
    handler = manager.handler_for("helper_template")
    assert handler is not None
    fetch = AsyncMock(wraps=handler.fetch)
    manager.register(bm.DomainHandler("helper_template", fetch, handler.restore))

    status, payload = await _call(
        component_core_client, "diff_backup", template_snapshot.name
    )

    assert status == 400
    assert payload["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "snapshot" in payload["error"]["message"].lower()
    assert payload["error"]["message"] != "'options'"
    fetch.assert_not_awaited()
