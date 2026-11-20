"""ha_restart replies before restarting when the server runs inside HA (#2691).

Replying first means Core's own refusals reach nobody, so anything Core would
refuse the restart for is read before the reply (#2632).

Core's own ``homeassistant.restart`` runs in every test, with its config check
and its database-upgrade refusal; only the stop handler it ends with is the
test's (``async_set_stop_handler``, Core's hook for that), so the test's Core
records the restart instead of stopping.
"""

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components.homeassistant import async_set_stop_handler
from homeassistant.components.recorder import Recorder
from homeassistant.config import SAFE_MODE_FILENAME
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import system_restart
from ha_mcp.tools.tools_system import SystemTools

INVALID_CONFIG = "homeassistant: [\n"


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """A config dir whose ``configuration.yaml`` passes Core's check."""
    (Path(hass_tmp_config_dir) / "configuration.yaml").write_text("", "utf-8")
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(system_restart, "EMBEDDED_RESTART_DELAY_S", 0)
    yield
    assert not system_restart.RESTART_TASKS


@pytest.fixture
def embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")


@pytest.fixture
async def restarts(hass: HomeAssistant, core_client: HomeAssistantClient) -> list[bool]:
    """Core with its restart service and config check; returns each stop it
    was asked for (True for a restart)."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "config", {})
    stops: list[bool] = []

    async def record_stop(hass: HomeAssistant, restart: bool) -> None:
        stops.append(restart)

    async_set_stop_handler(hass, record_stop)
    return stops


async def _set_up_backup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "backup", {})
    await hass.async_block_till_done()


async def _deferred_restart() -> None:
    await asyncio.gather(*system_restart.RESTART_TASKS)


def _break_config(hass: HomeAssistant) -> None:
    """Make ``configuration.yaml`` fail Core's check, without yielding to the
    event loop (a deferred restart must not run in between)."""
    Path(hass.config.path("configuration.yaml")).write_text(INVALID_CONFIG)


def _safe_mode_requested(hass: HomeAssistant) -> bool:
    return Path(hass.config.path(SAFE_MODE_FILENAME)).exists()


@pytest.mark.usefixtures("embedded")
async def test_embedded_replies_before_restart(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    restarts: list[bool],
) -> None:
    await _set_up_backup(hass)

    result = await SystemTools(core_client).ha_restart(confirm=True)

    assert result["success"] is True
    assert not any("database upgrade" in w for w in result["warnings"])
    assert not any("running backup" in w for w in result["warnings"])
    assert restarts == []
    await _deferred_restart()
    assert restarts == [True]
    assert not _safe_mode_requested(hass)


def _restart_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == system_restart.__name__]


@pytest.mark.usefixtures("embedded")
async def test_embedded_lost_reply_logs_one_line(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    restarts: list[bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def stop_before_replying(hass: HomeAssistant, restart: bool) -> None:
        # Stopping cancels the request that asked for it, as Core's shutdown
        # does: the client never gets a reply.
        raise asyncio.CancelledError

    async_set_stop_handler(hass, stop_before_replying)

    with caplog.at_level(logging.WARNING, logger=system_restart.__name__):
        await SystemTools(core_client).ha_restart(confirm=True)
        await _deferred_restart()

    (record,) = _restart_records(caplog)
    assert record.levelno == logging.WARNING
    assert "dispatched" in record.getMessage()
    assert record.exc_info is None


@pytest.mark.usefixtures("embedded")
async def test_embedded_refused_restart_keeps_traceback(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    restarts: list[bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deferred restart Core refuses never happened; the log keeps why."""
    with caplog.at_level(logging.WARNING, logger=system_restart.__name__):
        await SystemTools(core_client).ha_restart(confirm=True)
        # The configuration breaks between the reply and the restart, so
        # Core's own check refuses it.
        _break_config(hass)
        await _deferred_restart()

    (record,) = _restart_records(caplog)
    assert "failed" in record.getMessage()
    assert record.exc_info is not None
    assert restarts == []


@pytest.mark.usefixtures("embedded")
async def test_embedded_invalid_config_does_not_schedule(
    hass: HomeAssistant, core_client: HomeAssistantClient, restarts: list[bool]
) -> None:
    _break_config(hass)

    with pytest.raises(ToolError, match="Configuration is invalid"):
        await SystemTools(core_client).ha_restart(confirm=True)

    assert not system_restart.RESTART_TASKS
    assert restarts == []


async def test_external_awaits_restart_call(
    core_client: HomeAssistantClient,
    restarts: list[bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)

    result = await SystemTools(core_client).ha_restart(confirm=True)

    assert result["success"] is True
    assert restarts == [True]
    assert not system_restart.RESTART_TASKS


@pytest.mark.usefixtures("embedded")
async def test_embedded_db_upgrade_is_refused_not_reported_as_scheduled(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    restarts: list[bool],
) -> None:
    await _set_up_backup(hass)

    # A unit-test recorder cannot be mid-upgrade; Core sets this flag while
    # it migrates the database.
    with (
        patch.object(recorder_mock, "migration_in_progress", True),
        pytest.raises(ToolError, match="database upgrade"),
    ):
        await SystemTools(core_client).ha_restart(confirm=True)

    assert not system_restart.RESTART_TASKS
    assert restarts == []


@pytest.mark.usefixtures("embedded")
async def test_embedded_unreadable_recorder_restarts_and_says_unchecked(
    hass: HomeAssistant, core_client: HomeAssistantClient, restarts: list[bool]
) -> None:
    """Without the recorder Core does not answer ``recorder/info``."""
    await _set_up_backup(hass)

    result = await SystemTools(core_client).ha_restart(confirm=True)

    assert result["success"] is True
    assert any("database upgrade" in w for w in result["warnings"])
    await _deferred_restart()
    assert restarts == [True]


async def test_safe_mode_reaches_core(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    restarts: list[bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)

    await SystemTools(core_client).ha_restart(confirm=True, safe_mode=True)

    assert restarts == [True]
    assert _safe_mode_requested(hass)


@pytest.mark.usefixtures("embedded")
async def test_embedded_safe_mode_warns_server_stays_down(
    hass: HomeAssistant, core_client: HomeAssistantClient, restarts: list[bool]
) -> None:
    result = await SystemTools(core_client).ha_restart(confirm=True, safe_mode=True)

    assert any("next restarted normally" in w for w in result["warnings"])
    await _deferred_restart()
    assert restarts == [True]
    assert _safe_mode_requested(hass)


@pytest.mark.usefixtures("embedded")
async def test_embedded_busy_backup_is_refused_not_reported_as_scheduled(
    hass: HomeAssistant, core_client: HomeAssistantClient, restarts: list[bool]
) -> None:
    """Core's backup manager stays ``blocked`` until Home Assistant has
    started."""
    hass.set_state(CoreState.starting)
    await _set_up_backup(hass)

    with pytest.raises(ToolError, match="blocked"):
        await SystemTools(core_client).ha_restart(confirm=True)

    assert not system_restart.RESTART_TASKS
    assert restarts == []


@pytest.mark.usefixtures("embedded")
async def test_embedded_unreadable_backup_restarts_and_says_unchecked(
    core_client: HomeAssistantClient, restarts: list[bool]
) -> None:
    """Without ``backup`` Core does not answer ``backup/info``."""
    result = await SystemTools(core_client).ha_restart(confirm=True)

    assert result["success"] is True
    assert any("running backup" in w for w in result["warnings"])
    await _deferred_restart()
    assert restarts == [True]
