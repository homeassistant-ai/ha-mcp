"""Core's own hassio update entities, set up by Core's ``hassio`` integration
against the stand-in Supervisor of ``_tools_addons_core``.

Two apps are installed, each with an update pending: ``OTHER_APP``, and one
whose name is the title of Core's own update, read from the entity Core built.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network
from typing import Any
from uuid import UUID

import pytest
from aiohasupervisor.models import (
    Backup,
    BackupComplete,
    BackupContent,
    BackupLocationAttributes,
    BackupType,
    DockerNetwork,
    HomeAssistantInfo,
    HostInfo,
    InstalledAddon,
    Job,
    JobsInfo,
    LogLevel,
    MountsInfo,
    NetworkInfo,
    NewBackup,
    OSInfo,
    ResolutionInfo,
    RootInfo,
    StoreInfo,
    SupervisorState,
    UpdateChannel,
)
from aiohasupervisor.models.addons import AddonsList
from aiohasupervisor.models.backups import LOCATION_LOCAL_STORAGE, BackupList
from aiohasupervisor.models.ingress import IngressPanels
from homeassistant.components.hassio.const import ADDONS_COORDINATOR, MAIN_COORDINATOR
from homeassistant.components.hassio.entity import (
    HassioAddonEntity,
    HassioCoreEntity,
    HassioOSEntity,
    HassioSupervisorEntity,
)
from homeassistant.components.update import DATA_COMPONENT, UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from ._tools_addons_core import SLUG, Supervisor, app_info
from ._tools_updates_core import supervisor_info

OTHER_APP = "Mosquitto broker"
OTHER_SLUG = "core_mosquitto"
# The category each of Core's hassio entity classes stands for.
KINDS: tuple[tuple[type[Any], str], ...] = (
    (HassioCoreEntity, "core"),
    (HassioOSEntity, "os"),
    (HassioSupervisorEntity, "supervisor"),
    (HassioAddonEntity, "addons"),
)


def _supervisor_system(supervisor: Supervisor, core_versions: tuple[str, str]) -> None:
    """Supervisor's answers to what Core's hassio integration reads at setup,
    as Home Assistant OS gives them, built from aiohasupervisor's models."""
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
        ("/core/info", _core_info(installed, latest).to_dict()),
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
        supervisor.answer("GET", path, data)
    supervisor.answer("GET", "/supervisor/ping")
    supervisor.answer("POST", "/core/options")
    supervisor.answer("POST", "/supervisor/options")
    supervisor.answer("POST", "/reload_updates")


def _core_info(installed: str, latest: str) -> HomeAssistantInfo:
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


def _install_apps(supervisor: Supervisor, names: dict[str, str]) -> None:
    """Supervisor lists ``names`` (slug to name) as installed apps, each with
    an update pending, and answers each one's info and update."""
    apps = {
        slug: app_info(slug, name=name, version_latest="1.1.0")
        for slug, name in names.items()
    }
    listed = [InstalledAddon.from_dict(info) for info in apps.values()]
    supervisor.answer("GET", "/addons", AddonsList(addons=listed).to_dict())
    for slug, info in apps.items():
        supervisor.answer("GET", f"/addons/{slug}/info", info)
        supervisor.answer("POST", f"/store/addons/{slug}/update")


def hassio_entities(hass: HomeAssistant) -> list[UpdateEntity]:
    """The update entities Core's hassio integration added."""
    return [
        entity
        for entity in hass.data[DATA_COMPONENT].entities
        if entity.platform.platform_name == "hassio"
    ]


def hassio_entity(
    hass: HomeAssistant, kind: type[Any], title: str | None = None
) -> UpdateEntity:
    """The one hassio update entity of Core's class ``kind`` (with ``title``)."""
    [entity] = [
        e
        for e in hassio_entities(hass)
        if isinstance(e, kind) and (title is None or e.title == title)
    ]
    return entity


@pytest.fixture
def core_versions() -> tuple[str, str]:
    """The installed and the latest Core version Supervisor reports; a test
    parametrizes it to offer a Core update."""
    return "2026.11.2", "2026.11.2"


@pytest.fixture
async def hassio_updates(
    hass: HomeAssistant,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    core_versions: tuple[str, str],
) -> Supervisor:
    """Core's ``hassio`` integration set up against ``supervisor`` as on Home
    Assistant OS: Core's own Core, Supervisor, OS and app update entities.

    Set up before Core's HTTP server starts, which freezes its routes, so a
    test takes it ahead of ``core_client``. Supervisor's token is in the
    environment only while Core sets the integration up, so the server under
    test does not take itself for the app.
    """
    _supervisor_system(supervisor, core_versions)
    _install_apps(supervisor, {SLUG: "Placeholder app", OTHER_SLUG: OTHER_APP})
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.setenv("SUPERVISOR", supervisor.url)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "core-supervisor-token")
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "hassio", {})
    await hass.async_block_till_done()
    monkeypatch.delenv("SUPERVISOR_TOKEN")

    core_title = hassio_entity(hass, HassioCoreEntity).title
    assert core_title is not None
    _install_apps(supervisor, {SLUG: core_title, OTHER_SLUG: OTHER_APP})
    # The stand-in gives a queued answer once and keeps the last one, so the
    # first refresh still reads the list Core was set up with.
    for _ in range(2):
        await hass.data[ADDONS_COORDINATOR].async_refresh()
    await hass.async_block_till_done()
    assert hassio_entity(hass, HassioAddonEntity, core_title)
    return supervisor


async def offer_core_update(
    hass: HomeAssistant, supervisor: Supervisor, latest: str
) -> None:
    """Supervisor reports Core ``latest`` from now on; Core has not read it."""
    installed = hassio_entity(hass, HassioCoreEntity).installed_version
    assert installed is not None
    supervisor.answer("GET", "/core/info", _core_info(installed, latest).to_dict())
    # The stand-in gives a queued answer once and keeps the last one: this
    # refresh takes the answer Core was set up with, so Core's next read is
    # the new one.
    await hass.data[MAIN_COORDINATOR].async_refresh()
    assert hassio_entity(hass, HassioCoreEntity).latest_version == installed


async def serve_backups(hass: HomeAssistant, supervisor: Supervisor) -> None:
    """Core's ``backup`` integration, and Supervisor's answers to the backup a
    hassio update takes before it installs: the backup job, done at once, and
    the backup it wrote, built from aiohasupervisor's models."""
    job_id, slug = UUID(int=1), "c0ffee01"
    written = dt_util.utcnow()
    location = {LOCATION_LOCAL_STORAGE: BackupLocationAttributes(False, 1024)}
    job = Job(
        name="backup_manager_partial_backup",
        reference=slug,
        uuid=job_id,
        progress=100,
        stage=None,
        done=True,
        errors=[],
        created=written,
        child_jobs=[],
        extra=None,
    )
    details = BackupComplete(
        slug=slug,
        name="Update backup",
        date=written,
        type=BackupType.PARTIAL,
        location_attributes=location,
        compressed=True,
        supervisor_version="2026.11.0",
        homeassistant=None,
        addons=[],
        repositories=[],
        folders=[],
        homeassistant_exclude_database=None,
        extra=None,
    )
    listed = Backup(
        slug=slug,
        name=details.name,
        date=written,
        type=BackupType.PARTIAL,
        location_attributes=location,
        compressed=True,
        content=BackupContent(homeassistant=False, addons=[], folders=[]),
    )
    supervisor.answer("POST", "/backups/new/partial", NewBackup(job_id).to_dict())
    supervisor.answer("GET", f"/jobs/{job_id.hex}", job.to_dict())
    supervisor.answer("GET", f"/backups/{slug}/info", details.to_dict())
    supervisor.answer("GET", "/backups", BackupList(backups=[listed]).to_dict())
    assert await async_setup_component(hass, "backup", {})
    await hass.async_block_till_done()
