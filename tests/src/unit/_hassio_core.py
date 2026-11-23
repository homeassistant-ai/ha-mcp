"""Core's own ``hassio`` integration, set up as on Home Assistant OS against a
stand-in Supervisor (``SupervisorApi``).

Core's ``async_setup`` and its config entry run unchanged. The stand-in answers
what they read with aiohasupervisor's models, given to it as defaults.
"""

from __future__ import annotations

import logging
from ipaddress import IPv4Address, IPv4Network
from typing import Any, Protocol

import pytest
from aiohasupervisor.models import (
    DockerNetwork,
    HomeAssistantInfo,
    HostInfo,
    InstalledAddon,
    JobsInfo,
    LogLevel,
    MountsInfo,
    NetworkInfo,
    OSInfo,
    ResolutionInfo,
    RootInfo,
    StoreInfo,
    SupervisorState,
    UpdateChannel,
)
from aiohasupervisor.models.addons import AddonsList
from aiohasupervisor.models.ingress import IngressPanels
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ._tools_updates_core import supervisor_info

CORE_VERSION = "2026.11.2"


def supervisor_ok(data: Any = None) -> dict[str, Any]:
    """Supervisor's body for a request it accepts: ``data``, or ``{}`` without
    any, as its ``api_return_ok`` gives."""
    return {"result": "ok", "data": {} if data is None else data}


def supervisor_error(message: str) -> dict[str, Any]:
    """Supervisor's body for a request it refuses (``api_return_error``)."""
    return {"result": "error", "message": message}


class SupervisorApi(Protocol):
    """A Supervisor stand-in at ``url``. Each implementation decides which of
    its answers wins over a default, and whether defaults outlive setup."""

    url: str

    def default(self, method: str, path: str, data: Any = None) -> None:
        """Accept ``method path`` with ``data`` when nothing else answers it."""

    def default_refusal(
        self, method: str, path: str, message: str, status: int = 400
    ) -> None:
        """Refuse ``method path`` with ``message`` when nothing else answers it."""

    def forget_requests(self) -> None:
        """Drop the requests recorded so far."""


def core_info(installed: str, latest: str) -> HomeAssistantInfo:
    """``GET /core/info`` for Core ``installed`` with ``latest`` offered."""
    return HomeAssistantInfo(
        version=installed,
        version_latest=latest,
        update_available=installed != latest,
        machine="qemux86-64",
        ip_address=IPv4Address("172.30.32.1"),
        arch="amd64",
        image="ghcr.io/home-assistant/qemux86-64-homeassistant",
        boot=True,
        port=8123,
        ssl=False,
        watchdog=True,
        audio_input=None,
        audio_output=None,
        backups_exclude_database=False,
        duplicate_log_file=False,
    )


def _serve_system(supervisor: SupervisorApi, core_versions: tuple[str, str]) -> None:
    """Supervisor's answers to what Core's hassio integration reads at setup,
    as Home Assistant OS gives them."""
    installed, latest = core_versions
    root = RootInfo(
        supervisor="2026.11.0",
        homeassistant=installed,
        hassos="16.3",
        docker="28.0.0",
        hostname="homeassistant",
        operating_system="Home Assistant OS 16.3",
        features=[],
        machine="qemux86-64",
        machine_id=None,
        arch="amd64",
        state=SupervisorState.RUNNING,
        supported_arch=["amd64"],
        supported=True,
        channel=UpdateChannel.STABLE,
        logging=LogLevel.INFO,
        timezone="UTC",
    )
    os_info = OSInfo(
        version="16.3",
        version_latest="16.3",
        version_pending=None,
        update_available=False,
        board=None,
        boot="A",
        data_disk=None,
        boot_slots={},
    )
    host = HostInfo(
        agent_version=None,
        apparmor_version=None,
        chassis=None,
        virtualization=None,
        cpe=None,
        deployment=None,
        disk_free=10.0,
        disk_total=20.0,
        disk_used=10.0,
        disk_life_time=None,
        features=[],
        hostname="homeassistant",
        llmnr_hostname=None,
        kernel=None,
        operating_system="Home Assistant OS 16.3",
        timezone="UTC",
        dt_utc=None,
        dt_synchronized=None,
        use_ntp=None,
        startup_time=None,
        boot_timestamp=None,
        broadcast_llmnr=None,
        broadcast_mdns=None,
    )
    network = NetworkInfo(
        interfaces=[],
        docker=DockerNetwork(
            interface="hassio",
            address=IPv4Network("172.30.32.0/23"),
            gateway=IPv4Address("172.30.32.1"),
            dns=IPv4Address("172.30.32.3"),
        ),
        host_internet=True,
        supervisor_internet=True,
    )
    resolution = ResolutionInfo(
        suggestions=[], unsupported=[], unhealthy=[], issues=[], checks=[]
    )
    for path, data in (
        ("/info", root.to_dict()),
        ("/core/info", core_info(installed, latest).to_dict()),
        ("/supervisor/info", supervisor_info(UpdateChannel.STABLE)),
        ("/os/info", os_info.to_dict()),
        ("/host/info", host.to_dict()),
        ("/store", StoreInfo(addons=[], repositories=[]).to_dict()),
        ("/network/info", network.to_dict()),
        ("/ingress/panels", IngressPanels(panels={}).to_dict()),
        ("/mounts", MountsInfo(default_backup_mount=None, mounts=[]).to_dict()),
        ("/jobs/info", JobsInfo(ignore_conditions=[], jobs=[]).to_dict()),
        ("/resolution/info", resolution.to_dict()),
    ):
        supervisor.default("GET", path, data)
    supervisor.default("GET", "/supervisor/ping")
    supervisor.default("POST", "/core/options")
    supervisor.default("POST", "/supervisor/options")
    supervisor.default("POST", "/reload_updates")
    supervisor.default("POST", "/store/reload")
    # Core asks Supervisor to update itself while Home Assistant is not
    # onboarded; Supervisor refuses the version it already runs.
    supervisor.default_refusal(
        "POST", "/supervisor/update", "Version 2026.11.0 is already in use"
    )


def install_apps(supervisor: SupervisorApi, apps: list[dict[str, Any]]) -> None:
    """Supervisor lists ``apps`` (each one's ``/addons/<slug>/info``) as
    installed, and answers each one's info and update."""
    listed = [InstalledAddon.from_dict(info) for info in apps]
    supervisor.default("GET", "/addons", AddonsList(addons=listed).to_dict())
    for info in apps:
        supervisor.default("GET", f"/addons/{info['slug']}/info", info)
        supervisor.default("POST", f"/store/addons/{info['slug']}/update")


# Where Core logs a failed hassio setup: the integration and the entity domains
# it forwards to, a platform that fails to set up, and the config entry.
_SETUP_LOGGERS = (
    "homeassistant.components",
    "homeassistant.helpers.entity_platform",
    "homeassistant.config_entries",
)


class _Warnings(logging.Handler):
    """What Core logs at warning level or above."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"{record.name}: {record.getMessage()}")


async def set_up_hassio(
    hass: HomeAssistant,
    supervisor: SupervisorApi,
    monkeypatch: pytest.MonkeyPatch,
    *,
    core_versions: tuple[str, str] = (CORE_VERSION, CORE_VERSION),
    apps: list[dict[str, Any]] | None = None,
) -> None:
    """Set up Core's ``hassio`` integration against ``supervisor``, with
    ``apps`` installed.

    Supervisor's token is in the environment only while Core sets the
    integration up, so the server under test does not take itself for the app.
    The requests Core made while setting up are forgotten. Core logs a request
    the stand-in does not answer, or a platform that fails to set up, and still
    loads, so a warning it logs while setting up fails here.
    """
    _serve_system(supervisor, core_versions)
    install_apps(supervisor, apps or [])
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.setenv("SUPERVISOR", supervisor.url)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "core-supervisor-token")
    logged = _Warnings()
    watched = [logging.getLogger(name) for name in _SETUP_LOGGERS]
    for logger in watched:
        logger.addHandler(logged)
    try:
        assert await async_setup_component(hass, "hassio", {})
        await hass.async_block_till_done()
    finally:
        for logger in watched:
            logger.removeHandler(logged)
    assert not logged.messages, logged.messages
    [entry] = hass.config_entries.async_entries("hassio")
    assert entry.state is ConfigEntryState.LOADED, entry.reason
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    supervisor.forget_requests()
