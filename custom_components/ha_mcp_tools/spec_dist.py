"""Which distribution a server requirement installs, and whether it is the server.

The in-process server reads the installed version from that distribution's
metadata first when it ships ``ha_mcp``, so metadata another server
distribution left behind cannot mask it.
"""

from __future__ import annotations

import importlib.metadata
from urllib.parse import urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .const import DIST_NAME_DEV, DIST_NAME_STABLE


def spec_dist_name(spec: str) -> str | None:
    """Return the normalized distribution ``spec`` installs, or None.

    The requirement's name, or for a bare wheel URL the one its file name
    starts with (PEP 427); None for anything else that is no requirement,
    such as a bare tarball URL.
    """
    try:
        name = Requirement(spec).name
    except InvalidRequirement:
        filename = urlparse(spec.strip()).path.rsplit("/", 1)[-1]
        if not filename.endswith(".whl"):
            return None
        name = filename.split("-", 1)[0]
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
