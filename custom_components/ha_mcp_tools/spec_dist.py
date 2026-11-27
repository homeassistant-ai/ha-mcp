"""Which server requirement the entry installs, and which distribution that is.

The in-process server reads the installed version from that distribution's
metadata first when it ships ``ha_mcp``, so metadata another server
distribution left behind cannot mask it.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .const import (
    DEFAULT_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    OPT_PIP_SPEC,
)
from .server_pin import server_pins


def pip_spec_override(options: Mapping[str, Any]) -> str:
    """Return the server entry's pip-spec override, or "" when it sets none.

    ``DEFAULT_PIP_SPEC`` in the field means no override: the options flow
    normalizes it away on save, and this keeps legacy and directly written
    entries right too.
    """
    raw = str(options.get(OPT_PIP_SPEC) or "").strip()
    return raw if raw != DEFAULT_PIP_SPEC else ""


async def async_paired_server_requirement(hass: HomeAssistant) -> str | None:
    """Return the server requirement this component release pins, or None.

    Every component release the Marketplace delivers pins the ``ha-mcp`` (pre-release:
    ``ha-mcp-dev``) build it was released with in ``manifest.json``'s ``requirements``, which Home
    Assistant installs before setting the integration up. A copy taken from
    master pins the last release; only a hand-edited manifest carries none.
    """
    integration = await async_get_integration(hass, DOMAIN)
    return next(iter(server_pins(integration.requirements)), None)


def effective_pip_spec(override: str, pin: str | None) -> str:
    """Return the server requirement bring-up installs.

    An explicit override wins (any pip requirement string — a version pin,
    a GitHub tarball URL, a ``file://`` wheel — for PR or pre-release
    testing). Otherwise the requirement this component release pins in its
    manifest, so the server that runs is the one the Marketplace delivered
    with the component. A hand-edited manifest without a pin falls back to the
    bare ``ha-mcp`` distribution (``DEFAULT_PIP_SPEC``).

    ``override`` is :func:`pip_spec_override`'s result ("" for none). ``pin`` is
    None when the manifest pins no server or has not been read.
    """
    return override or pin or DEFAULT_PIP_SPEC


async def async_effective_pip_spec(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> str:
    """Return :func:`effective_pip_spec` for ``options`` and the manifest pin.

    ``server_entry``'s ``install_pending`` compares it with the spec bring-up
    recorded.
    """
    override = pip_spec_override(options)
    # An override decides alone, so the manifest is not read for it.
    pin = None if override else await async_paired_server_requirement(hass)
    return effective_pip_spec(override, pin)


def spec_dist_name(spec: str) -> str | None:
    """Return the normalized distribution ``spec`` installs, or None.

    The requirement's name, or for a bare wheel or sdist URL the one its file
    name starts with (PEP 427, PEP 625); None for anything else that is no
    requirement, such as a repository archive URL (``.../archive/.../head.tar.gz``).
    """
    try:
        name = Requirement(spec).name
    except InvalidRequirement:
        path = urlparse(spec.strip()).path
        filename = path.rsplit("/", 1)[-1]
        if filename.endswith(".whl"):
            name = filename.split("-", 1)[0]
        elif filename.endswith(".tar.gz") and "/archive/" not in path:
            # An sdist names {name}-{version}; a repository archive names a ref.
            name, _, version = filename.removesuffix(".tar.gz").partition("-")
            try:
                Version(version)
            except InvalidVersion:
                return None
        else:
            return None
    return canonicalize_name(name)


def version_dist(spec: str) -> str:
    """Distribution whose metadata is read first for the installed version.

    The one ``spec`` installs, whatever its name. ``ha-mcp`` for a spec that
    names none: a repository tarball URL installs under that name.
    """
    return spec_dist_name(spec) or DIST_NAME_STABLE


def provides_server(dist_name: str) -> bool:
    """Return whether ``dist_name``'s version can be the server's (blocking).

    Always for ``ha-mcp`` and ``ha-mcp-dev``: an editable install may list none
    of the package's files. Any other name only when it ships ``ha_mcp``, so an
    override naming an unrelated distribution is not read as the server. Mirrors
    ``ha_mcp._version._owns_ha_mcp``, which this process must not import: a
    declared ``top_level.txt`` wins, otherwise the file list decides.
    """
    if dist_name in (DIST_NAME_STABLE, DIST_NAME_DEV):
        return True
    try:
        dist = importlib.metadata.distribution(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return False
    declared = (dist.read_text("top_level.txt") or "").split()
    if declared:
        return "ha_mcp" in declared
    return any(path.parts[:1] == ("ha_mcp",) for path in dist.files or ())
