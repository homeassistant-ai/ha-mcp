"""Theme / accessibility preference persistence for the settings UI.

The browser keeps these in localStorage for synchronous pre-paint reads,
but localStorage is origin-scoped and the stdio settings sidecar binds a
random free port per spawn — every session is a fresh origin that starts
empty. The server-side copy here survives that: POSTs land in
``theme_prefs.json`` next to the other settings files, and the page
handler seeds them back into the served HTML (``server-prefs`` head
script) so a fresh origin paints with the user's saved choices.

Kept as a leaf module (no imports from the settings_ui package) so the
handler families and ``__init__`` can depend on it without cycles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from ..utils.data_paths import get_data_dir

logger = logging.getLogger(__name__)

_THEME_PREFS_FILENAME = "theme_prefs.json"

# Allowed values mirror exactly what the settings UI offers; anything else
# (hand-edited file, hand-rolled request) is dropped rather than persisted
# and re-injected into the served page.
_THEME_PREF_VALUES: dict[str, tuple[str, ...]] = {
    "theme": ("auto", "light", "dark"),
    "fontSize": ("100", "115", "130", "150"),
    "contrast": ("normal", "high"),
    "shade": ("off-white", "paper", "gray", "pure"),
}
_CUSTOM_COLOR_PARTS = ("bg", "text", "accent")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Same single-loop rationale as ``_OVERRIDE_FILE_LOCK`` in _supervisor;
# separate lock because it serializes a different file (``theme_prefs.json``).
_THEME_PREFS_LOCK: asyncio.Lock | None = None

# Keys already warned about by ``_load_theme_prefs`` — it runs on every
# page view, so each invalid entry logs once per process, not per view.
_WARNED_DROPPED_THEME_PREFS: set[str] = set()


def _get_theme_prefs_lock() -> asyncio.Lock:
    global _THEME_PREFS_LOCK
    if _THEME_PREFS_LOCK is None:
        _THEME_PREFS_LOCK = asyncio.Lock()
    return _THEME_PREFS_LOCK


def _sanitize_theme_prefs(raw: object) -> dict[str, str] | None:
    """Reduce ``raw`` to the known pref keys with offered values.

    Returns ``None`` when ``raw`` is not a JSON object at all. The
    ``custom`` value is itself a JSON string of hex colors; it is parsed,
    filtered to valid ``#rrggbb`` parts, and re-serialized so nothing but
    vetted color literals ever reaches the persisted file or the served
    HTML. An explicit empty string is kept — it records "cleared".
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, str] = {}
    for key, allowed in _THEME_PREF_VALUES.items():
        value = raw.get(key)
        if isinstance(value, str) and value in allowed:
            out[key] = value
    custom = raw.get("custom")
    if custom == "":
        out["custom"] = ""
    elif isinstance(custom, str):
        try:
            parsed = json.loads(custom)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            clean = {
                part: value
                for part, value in parsed.items()
                if part in _CUSTOM_COLOR_PARTS
                and isinstance(value, str)
                and _HEX_COLOR_RE.match(value)
            }
            if clean:
                out["custom"] = json.dumps(clean, separators=(",", ":"))
    return out


def _load_theme_prefs() -> dict[str, str]:
    """Best-effort read of the persisted theme prefs.

    Missing file is the normal first-run state; corrupt or unreadable
    files degrade to client-side defaults (these prefs are cosmetic and
    trivially re-settable, unlike feature flags there is nothing to
    protect by refusing).
    """
    path = get_data_dir() / _THEME_PREFS_FILENAME
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Cannot read theme prefs at %s", path, exc_info=True)
        return {}
    sanitized = _sanitize_theme_prefs(raw) or {}
    if isinstance(raw, dict):
        # Hand-edited values outside the offered sets are dropped on load
        # AND overwritten by the next save's RMW merge — leave a trail so
        # the edit's author can see why their value never sticks. Warn
        # once per key per process: _render_settings_html() calls this on
        # every page view, and a permanently stale key (e.g. left behind
        # by a future version) must not spam the log forever (#1574
        # review).
        dropped = set(raw) - set(sanitized) - _WARNED_DROPPED_THEME_PREFS
        if dropped:
            _WARNED_DROPPED_THEME_PREFS.update(dropped)
            logger.warning("Ignoring invalid theme pref entries: %s", sorted(dropped))
    return sanitized
