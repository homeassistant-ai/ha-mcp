"""Maintenance policy for intentionally best-effort locale catalogs."""

from __future__ import annotations

# Klingon is a novelty translation maintained by hand. It must never consume
# machine-translation quota or block delivery for supported localizations.
BEST_EFFORT_LOCALES = frozenset({"tlh"})


def is_best_effort_locale(locale: str) -> bool:
    """Return whether ``locale`` is isolated from hard localization gates."""
    normalized = locale.lower().replace("_", "-")
    return normalized in BEST_EFFORT_LOCALES
