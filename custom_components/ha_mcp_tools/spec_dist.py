"""Which distribution a server requirement installs.

The in-process server reads the installed version from that distribution's
metadata first, so metadata another server distribution left behind cannot mask
it (#2765): bring-up, the post-start staleness check and the diagnostics
download all read through :func:`version_dist`.
"""

from __future__ import annotations

from urllib.parse import urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .const import DIST_NAME_STABLE


def spec_dist_name(spec: str) -> str | None:
    """Return the normalized distribution ``spec`` installs, or None.

    The requirement's name, or for a bare wheel URL the one its file name
    starts with (PEP 427); None for any other bare URL.
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
