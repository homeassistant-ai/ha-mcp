"""Which manifest requirement is the component's ha-mcp server pin.

The one rule the component, the release stamping script, the CI version gate
and the E2E seed share. Standard library only and no package-relative imports,
so the scripts and CI load this file by path without Home Assistant.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# The distributions a server pin may name; one is installed at a time.
SERVER_DISTS = ("ha-mcp", "ha-mcp-dev")

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def server_dist(requirement: str) -> str | None:
    """Return the server distribution ``requirement`` names, or None."""
    match = _NAME.match(requirement)
    if match is None:
        return None
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    return name if name in SERVER_DISTS else None


def server_pins(requirements: Iterable[str]) -> list[str]:
    """Return the requirements that pin a server distribution."""
    return [r for r in requirements if server_dist(r) is not None]
