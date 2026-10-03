"""Loading and scrubbing of resolved ``secrets.yaml`` values."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import yaml  # type: ignore[import-untyped]
from homeassistant.core import HomeAssistant

# All ws_* modules log through the websocket_api logger, so one logger
# setting covers the whole command surface.
_LOGGER = logging.getLogger(__package__)


def _load_secret_scrub(hass: HomeAssistant) -> tuple[frozenset[str], bool]:
    """Load the ``secrets.yaml`` scrub values, plus a ``degraded`` flag.

    Returns ``(values, degraded)``. ``values`` are the plaintext string forms of the
    instance's ``secrets.yaml`` scalars; they scrub resolved ``!secret`` plaintext
    out of two surfaces: the config-body match corpus (so ``ha_search`` cannot be a
    probe oracle — a query equal to a suspected secret confirmed via
    ``match_in_config``) and the ``options`` emitted by ``config_entries`` /
    ``helpers_list`` (so a resolved secret never leaves the component).

    Both string AND numeric scalars are collected as their ``str()`` form: an
    unquoted ``alarm_code: 1234`` is a YAML int, and a config-entry option can carry
    that secret back as an int leaf, so the scrub must be able to match it whether it
    arrives as ``1234`` or ``"1234"``. bool scalars are excluded ("True"/"False" are
    never credentials and would over-redact).

    ``degraded`` is True ONLY when a ``secrets.yaml`` is PRESENT but could not be
    read/parsed: the scrub then silently turns OFF, and a caller emitting options can
    surface ``degraded`` so an unredacted response is not mistaken for a cleanly
    scrubbed one. An ABSENT ``secrets.yaml`` (the common case) is NOT degraded —
    there is simply nothing to scrub.

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
    try:
        path = path_fn("secrets.yaml")
        if not path:
            return frozenset(), False
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError:
        # Expected: many instances have no secrets.yaml — nothing to scrub.
        return frozenset(), False
    except Exception:
        # Present-but-unreadable / malformed / permission error: unexpected, so warn
        # once (this runs once per call) AND report degraded so the emission callers
        # can signal that options were NOT redacted, rather than raising into the WS
        # handler.
        _LOGGER.warning(
            "Could not read secrets.yaml for the secret-scrub; continuing WITHOUT "
            "redaction (emitted options may be unredacted)",
            exc_info=True,
        )
        return frozenset(), True
    if not isinstance(raw, dict):
        return frozenset(), False
    return _collect_secret_strings(raw), False


def _collect_secret_strings(raw: dict[Any, Any]) -> frozenset[str]:
    """Plaintext ``str()`` forms of ``secrets.yaml`` scalars (str/int/float).

    bool scalars are excluded ("True"/"False" are never credentials and would
    over-redact); empty strings are dropped. See :func:`_load_secret_scrub`.
    """
    values: set[str] = set()
    for v in raw.values():
        if isinstance(v, bool):
            continue
        if isinstance(v, str):
            if v:
                values.add(v)
        elif isinstance(v, (int, float)):
            values.add(str(v))
    return frozenset(values)


def _load_secret_values(hass: HomeAssistant) -> frozenset[str]:
    """The ``secrets.yaml`` scrub set (see :func:`_load_secret_scrub`); degraded dropped.

    The ``search`` corpus scrub is best-effort and does not surface the degraded
    signal (its filtering degrading open is the pre-PR behaviour); the
    ``config_entries`` / ``helpers_list`` emission preps call
    :func:`_load_secret_scrub` directly so they can surface it.
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
        return "**redacted**"
    return value
