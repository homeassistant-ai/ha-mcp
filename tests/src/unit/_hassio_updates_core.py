"""Core's own hassio update entities, set up by Core's ``hassio`` integration
against the stand-in Supervisor of ``_tools_addons_core``.

Two apps are installed, each with an update pending: ``OTHER_APP``, and one
whose name is the title of Core's own update, read from the entity Core built.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from aiohasupervisor.models import (
    Backup,
    BackupComplete,
    BackupContent,
    BackupLocationAttributes,
    BackupType,
    Job,
    NewBackup,
)
from aiohasupervisor.models.backups import LOCATION_LOCAL_STORAGE, BackupList
from homeassistant.components.hassio.const import ADDONS_COORDINATOR
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

from ._hassio_core import CORE_VERSION, core_info, install_apps, set_up_hassio
from ._tools_addons_core import SLUG, Supervisor, app_info

OTHER_APP = "Mosquitto broker"
OTHER_SLUG = "core_mosquitto"
# The category each of Core's hassio entity classes stands for.
KINDS: tuple[tuple[type[Any], str], ...] = (
    (HassioCoreEntity, "core"),
    (HassioOSEntity, "os"),
    (HassioSupervisorEntity, "supervisor"),
    (HassioAddonEntity, "addons"),
)


def _apps(names: dict[str, str]) -> list[dict[str, Any]]:
    """Each slug's app info with its name, an update pending."""
    return [
        app_info(slug, name=name, version_latest="1.1.0")
        for slug, name in names.items()
    ]


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
    return CORE_VERSION, CORE_VERSION


@pytest.fixture
async def hassio_updates(
    hass: HomeAssistant,
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    core_versions: tuple[str, str],
) -> Supervisor:
    """Core's ``hassio`` integration set up against ``supervisor`` as on Home
    Assistant OS: Core's own Core, Supervisor, OS and app update entities."""
    assert await async_setup_component(hass, "config", {})
    await set_up_hassio(
        hass,
        supervisor,
        monkeypatch,
        core_versions=core_versions,
        apps=_apps({SLUG: "Placeholder app", OTHER_SLUG: OTHER_APP}),
    )

    core_title = hassio_entity(hass, HassioCoreEntity).title
    assert core_title is not None
    install_apps(supervisor, _apps({SLUG: core_title, OTHER_SLUG: OTHER_APP}))
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
    supervisor.default("GET", "/core/info", core_info(installed, latest).to_dict())


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
