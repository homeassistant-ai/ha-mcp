"""Classifier for the scene-degradation fragments ``ha_search`` reports (#2292).

Lives in its own stdlib-only module so the e2e assertion helper and the unit
test that pins the matcher's edge cases share one implementation: the unit test
loads this file by path, without pulling in the e2e package (pytest fixtures,
relative imports, a Docker-shaped conftest) that it has no business importing.
"""

from __future__ import annotations

# Any fragment carrying one of these is a degradation gone wrong, whatever else
# it mentions — checked before the structural shapes so a failed-fetch fragment
# whose ``e.g.`` sample happens to name a YAML-defined scene is still rejected.
_FAILURE_MARKERS = (
    "fetch raised",
    "timed out",
    "registry",
    "not scanned (per-id fetch",
)


def is_structural_scene_fragment(text: str) -> bool:
    """True only for the two structural scene classifications (#2292).

    Anchored on the fragments' distinctive message shapes from
    ``_apply_scene_partial_flag`` rather than loose substrings.

    - YAML-defined gap: "per-id config endpoint returned 404 — these are
      likely YAML-defined scenes ..."
    - Informational integration-managed note (an idless YAML scene has no
      registry unique_id, so the legacy walk classifies it integration-managed
      and scores it by attribute — match status KNOWN, just config-less):
      "... scored by attribute only (no per-id fetch)."
    """
    if any(marker in text for marker in _FAILURE_MARKERS):
        return False
    return (
        "per-id config endpoint returned 404" in text and "YAML-defined scenes" in text
    ) or text.rstrip().endswith("scored by attribute only (no per-id fetch).")
