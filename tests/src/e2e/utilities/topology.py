"""Lane-topology predicates shared across the e2e suite (#2292).

The no-tools lanes (``E2E_NO_TOOLS_ENTRY=1``) come in two shapes, and tests that
degrade differently between them need to tell the shapes apart without each
re-deriving the env logic. See the "No-tools lanes" section of
``tests/AGENTS.md`` for the full lane table.
"""

from __future__ import annotations

import os
from typing import Any

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


def combined_topologies() -> bool:
    """Whether this session covers absence followed by live installation."""
    value = os.environ.get("E2E_COMBINE_TOPOLOGIES", "").strip().lower()
    if value not in _TRUE_VALUES | _FALSE_VALUES:
        raise RuntimeError(f"Unrecognized E2E_COMBINE_TOPOLOGIES value: {value!r}")
    return value in _TRUE_VALUES


def item_topology(item: Any) -> str | None:
    """Return a collected item's phase, before fixtures have changed state."""
    callspec = getattr(item, "callspec", None)
    return callspec.params.get("e2e_topology") if callspec is not None else None


def item_tools_entry_absent(item: Any) -> bool:
    phase = item_topology(item)
    return phase == "topology_without_tools" if phase else tools_entry_absent()


def tools_entry_absent() -> bool:
    """True on the no-tools lanes, where the File & YAML Tools entry is absent.

    ``E2E_NO_TOOLS_ENTRY`` is normalized (stripped, lower-cased) and anything
    outside the recognized true/false spellings RAISES rather than defaulting.
    Guessing "off" for an unrecognized value would silently re-run the ordinary
    topology on a lane whose entire purpose is to test the no-tools one, and the
    lane would report green while covering nothing new (#2292).
    """
    raw = os.environ.get("E2E_NO_TOOLS_ENTRY", "")
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise RuntimeError(
        f"E2E_NO_TOOLS_ENTRY={raw!r} is not a recognized boolean "
        f"(true: {sorted(_TRUE_VALUES)}; false: {sorted(_FALSE_VALUES - {''})}, "
        "or unset). Refusing to guess: reading it as 'off' would silently "
        "re-run the ordinary topology on a lane that exists to test the "
        "no-tools one, and every downstream marker and assertion keys off "
        "this value (#2292)."
    )


def component_surface_available(*, no_tools_entry: bool | None = None) -> bool:
    """True wherever a live ha_mcp_tools component surface can answer.

    The ordinary lanes all have the component's tools entry, and the embedded
    lanes add the in-process server entry on top of it. Of the no-tools
    lanes, only the two ``embedded`` shapes keep an active config entry: their
    in-process server entry still registers the ``ha_mcp_tools/*`` WebSocket
    surface (#2291), so shared component capabilities — a config entry's
    ``unique_id``, the component-side scans — still answer there even though the
    privileged filesystem / YAML services are gone. The other no-tools shapes
    (plain container, HAOS inaddon) start without an active component entry, so
    those capabilities have no source and server code must degrade to whatever
    the legacy REST path can see. On HAOS inaddon a test can still enable the
    baked server entry later in the session; this predicate does not track it.
    """
    # Same normalization as conftest's backend selectors, so a padded or
    # differently-cased env value can't make this predicate disagree with
    # the dispatch that actually selects the embedded backend.
    return (
        not (tools_entry_absent() if no_tools_entry is None else no_tools_entry)
        or os.environ.get("E2E_BACKEND", "").strip().lower() == "embedded"
        or os.environ.get("HAOS_TEST_MODE", "").strip().lower() == "embedded"
    )
