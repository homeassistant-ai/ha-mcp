"""Loading and scrubbing of resolved ``secrets.yaml`` values."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping
from typing import Any

import yaml  # type: ignore[import-untyped]
from annotatedyaml import SECRET_YAML
from homeassistant.core import HomeAssistant

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(__package__)

REDACTED = "**redacted**"


def _load_secret_scrub(
    hass: HomeAssistant, files: Iterable[str] = ()
) -> tuple[frozenset[str], bool]:
    """Load the ``secrets.yaml`` scrub values, plus a ``degraded`` flag.

    Returns ``(values, degraded)``. ``values`` are the plaintext string forms of the
    instance's ``secrets.yaml`` scalars; they scrub resolved ``!secret`` plaintext
    out of two surfaces: the config-body match corpus (so ``ha_search`` cannot be a
    probe oracle — a query equal to a suspected secret confirmed via
    ``match_in_config``) and the ``options`` emitted by ``config_entries`` /
    ``helpers_list`` / a helper ``search`` (so a resolved secret never leaves the
    component).

    Both string AND numeric scalars are collected as their ``str()`` form: an
    unquoted ``alarm_code: 1234`` is a YAML int, and a config-entry option can carry
    that secret back as an int leaf, so the scrub must be able to match it whether it
    arrives as ``1234`` or ``"1234"``. bool scalars are excluded ("True"/"False" are
    never credentials and would over-redact).

    ``files`` are YAML files whose ``!secret`` values Core resolved: the
    ``secrets.yaml`` files Core's loader reads for them are loaded too (see
    :func:`_secret_files`).

    ``degraded`` is True ONLY when a ``secrets.yaml`` is PRESENT but could not be
    read/parsed: the scrub then silently turns OFF, and a caller can surface
    ``degraded`` so unredacted options or an unscrubbed match corpus are not
    mistaken for cleanly scrubbed ones. An ABSENT ``secrets.yaml`` (the common
    case) is NOT degraded — there is simply nothing to scrub.

    Defensive by design — never raises into the WS handler. Loaded off the event loop
    by the async preps once per call, never cached, so an edited ``secrets.yaml``
    applies on the next call. ``secrets.yaml`` is a flat ``key: value`` mapping with
    no custom tags, so the plain ``yaml.safe_load`` (not HA's ``!secret``/
    ``!include`` loader) reads it correctly.
    """
    config = getattr(hass, "config", None)
    path_fn = getattr(config, "path", None)
    if not callable(path_fn):
        return frozenset(), False
    values: set[str] = set()
    degraded = False
    for path in _secret_files(path_fn(SECRET_YAML), files):
        try:
            with open(path, encoding="utf-8") as handle:
                raw = yaml.safe_load(handle)
        except FileNotFoundError:
            # Expected: many instances have no secrets.yaml — nothing to scrub.
            continue
        except Exception:
            # Present-but-unreadable / malformed / permission error: unexpected, so
            # warn AND report degraded so the emission callers can signal that
            # options were NOT redacted, and the helper search that its match corpus
            # was not scrubbed, rather than raising into the WS handler.
            _LOGGER.warning(
                "Could not read %s for the secret-scrub; continuing WITHOUT its "
                "values (emitted config may be unredacted, search matches unscrubbed)",
                path,
                exc_info=True,
            )
            degraded = True
            continue
        if isinstance(raw, dict):
            values |= _collect_secret_strings(raw)
    return frozenset(values), degraded


def _secret_files(root_file: str | None, files: Iterable[str]) -> list[str]:
    """The config directory's ``secrets.yaml`` and, for each of ``files``, the
    ones Core's loader also looks a ``!secret`` up in: each directory from the
    file's own up to the config directory."""
    if not root_file:
        return []
    root = os.path.dirname(root_file)
    found = [root_file]
    for name in files:
        folder = os.path.dirname(os.path.abspath(name))
        while folder.startswith(root + os.sep):
            found.append(os.path.join(folder, SECRET_YAML))
            folder = os.path.dirname(folder)
    return list(dict.fromkeys(found))


def _collect_secret_strings(raw: dict[Any, Any]) -> frozenset[str]:
    """Plaintext ``str()`` forms of ``secrets.yaml`` scalars (str/int/float),
    those inside a mapping or list secret included.

    bool scalars are excluded ("True"/"False" are never credentials and would
    over-redact); empty strings are dropped. See :func:`_load_secret_scrub`.
    """
    values: set[str] = set()
    pending = list(raw.values())
    # A YAML alias can make a mapping or list contain itself.
    seen: set[int] = set()
    while pending:
        v = pending.pop()
        if isinstance(v, (dict, list)):
            if id(v) not in seen:
                seen.add(id(v))
                pending.extend(v.values() if isinstance(v, dict) else v)
        elif isinstance(v, str):
            if v:
                values.add(v)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            values.add(str(v))
    return frozenset(values)


# Added to a helper ``search``'s ``warnings`` when the scrub degraded: the first
# when the response emits flow-helper options (``include_config``), the second
# when it does not. Either way the whole match corpus went unscrubbed. The server
# merges them into ``ha_search``.
SCRUB_DEGRADED_WARNING = (
    "secrets.yaml could not be read, so flow-helper options in this response "
    "were not scrubbed of resolved !secret values, and a match_in_config hit "
    "may confirm a secret value."
)
SCRUB_DEGRADED_MATCH_WARNING = (
    "secrets.yaml could not be read, so this search matched config bodies "
    "without the !secret scrub; a match_in_config hit may confirm a secret value."
)


def _load_secret_values(hass: HomeAssistant) -> frozenset[str]:
    """The ``secrets.yaml`` scrub set (see :func:`_load_secret_scrub`); degraded dropped.

    :func:`_search_prep` uses it for a ``search`` without the helper surface, which
    only filters its match corpus and reports no scrub warnings. The
    ``config_entries`` / ``helpers_list`` / helper ``search`` preps call
    :func:`_load_secret_scrub` directly so they can surface the degraded signal.
    """
    values, _degraded = _load_secret_scrub(hass)
    return values


def _scrub_secret_values(value: Any, secret_values: frozenset[str]) -> Any:
    """Recursively replace any leaf equal to a known secret with ``"**redacted**"``.

    Walks ``Mapping`` values, list/tuple items, and scalar leaves of the (already
    ``_plainify``'d) options structure. A scalar leaf whose plaintext form
    (``str(leaf)``) exactly equals a ``secrets.yaml`` value is redacted, so a
    resolved ``!secret`` never leaves the component whether an integration persists
    it as a string OR as the original scalar (an int ``alarm_code`` leaves as an int
    leaf). ``bool`` leaves are never secrets and pass through unchanged (their
    ``"True"``/``"False"`` form would over-redact). An empty ``secret_values`` is a
    no-op (fast path). Handles ``Mapping``/``tuple`` directly so it is correct even
    if applied to a structure that skipped :func:`_plainify`.
    """
    if not secret_values:
        return value
    if isinstance(value, Mapping):
        return {k: _scrub_secret_values(v, secret_values) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_secret_values(v, secret_values) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)) and str(value) in secret_values:
        return REDACTED
    return value
