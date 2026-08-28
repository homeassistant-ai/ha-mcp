"""Lane-topology predicates shared across the e2e suite (#2292).

The no-tools lanes (``E2E_NO_TOOLS_ENTRY=1``) come in two shapes, and tests that
degrade differently between them need to tell the shapes apart without each
re-deriving the env logic. See the "No-tools lanes" section of
``tests/AGENTS.md`` for the full lane table.
"""

from __future__ import annotations

import os


def tools_entry_absent() -> bool:
    """True on the no-tools lanes, where the File & YAML Tools entry is absent."""
    return os.environ.get("E2E_NO_TOOLS_ENTRY") == "1"


def component_surface_available() -> bool:
    """True wherever a live ha_mcp_tools component surface can answer.

    Every ordinary lane has the component with both entries. Of the no-tools
    lanes, only the two ``embedded`` shapes keep an active config entry: their
    in-process server entry still registers the ``ha_mcp_tools/*`` WebSocket
    surface (#2291), so shared component capabilities — a config entry's
    ``unique_id``, the component-side scans — still answer there even though the
    privileged filesystem / YAML services are gone. The other no-tools shapes
    (plain container, HAOS inaddon) have no active component entry at all, so
    those capabilities have no source and server code must degrade to whatever
    the legacy REST path can see.
    """
    # Same normalization as conftest's backend selectors, so a padded or
    # differently-cased env value can't make this predicate disagree with
    # the dispatch that actually selects the embedded backend.
    return (
        not tools_entry_absent()
        or os.environ.get("E2E_BACKEND", "").strip().lower() == "embedded"
        or os.environ.get("HAOS_TEST_MODE", "").strip().lower() == "embedded"
    )
