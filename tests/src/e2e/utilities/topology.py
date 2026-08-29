"""Lane-topology predicates shared across the e2e suite (#2292).

The no-tools lanes (``E2E_NO_TOOLS_ENTRY=1``) come in two shapes, and tests that
degrade differently between them need to tell the shapes apart without each
re-deriving the env logic. See the "No-tools lanes" section of
``tests/AGENTS.md`` for the full lane table.
"""

from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


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


def component_surface_available() -> bool:
    """True wherever a live ha_mcp_tools component surface can answer.

    The ordinary lanes all have the component's tools entry, and the embedded
    lanes add the in-process server entry on top of it. Of the no-tools
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
