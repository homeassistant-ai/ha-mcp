"""Optional, compatible QuickJS dependency for dashboard advice only."""

from __future__ import annotations

import importlib.util
import logging
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_LOGGER = logging.getLogger(__name__)
# Function's private executor/context contract is tested against this wrapper.
QUICKJS_VERSION = "0.17.0.1"
QUICKJS_REQUIREMENT = f"quickjs-ng=={QUICKJS_VERSION}"


def _provider_state() -> str:
    """Never replace a provider or violate another installed package's needs."""
    versions = {}
    for name in ("quickjs", "quickjs-ng"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    if "quickjs" in versions:
        raise ValueError("the quickjs package already owns the quickjs module")
    if "quickjs-ng" in versions and versions["quickjs-ng"] != QUICKJS_VERSION:
        raise ValueError("an existing quickjs-ng version must not be replaced")
    _check_consumers()
    if "quickjs-ng" in versions:
        return "ready"
    if importlib.util.find_spec("quickjs") is not None:
        raise ValueError("an unmanaged quickjs module is already present")
    return "missing"


def _check_consumers() -> None:
    for dist in metadata.distributions():
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            name = canonicalize_name(requirement.name)
            if name == "quickjs" or (
                name == "quickjs-ng" and QUICKJS_VERSION not in requirement.specifier
            ):
                raise ValueError(
                    "another installed package requires a different QuickJS provider"
                )


def _check_core_constraints() -> None:
    """Honor Core's constraints even when the package is already installed."""
    import homeassistant

    constraints = Path(homeassistant.__file__).parent / "package_constraints.txt"
    for line in constraints.read_text(encoding="utf-8").splitlines():
        if not line.strip().lower().startswith("quickjs"):
            continue
        requirement = Requirement(line.split("#", 1)[0].strip())
        if requirement.marker and not requirement.marker.evaluate():
            continue
        if (
            canonicalize_name(requirement.name) == "quickjs"
            or QUICKJS_VERSION not in requirement.specifier
        ):
            raise ValueError("Home Assistant requires a different QuickJS provider")


async def async_ensure_runtime(hass: Any) -> bool:
    """First-use install failure affects advice, never integration startup."""
    try:
        await hass.async_add_executor_job(_check_core_constraints)
        state = await hass.async_add_executor_job(_provider_state)
        if state == "missing":
            from homeassistant.requirements import async_process_requirements

            # HA owns pip locking, installation location and Core constraints.
            await async_process_requirements(
                hass,
                "ha_mcp_tools_dashboard_cards",
                [QUICKJS_REQUIREMENT],
                is_built_in=False,
            )
        installed: str = await hass.async_add_executor_job(_provider_state)
        return installed == "ready"
    except Exception:
        _LOGGER.warning(
            "Dashboard card advice is unavailable: compatible QuickJS could not be loaded",
            exc_info=True,
        )
        return False
