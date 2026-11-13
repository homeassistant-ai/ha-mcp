"""Unit tests for the structural scene-fragment matcher (#2292).

``is_structural_scene_fragment`` decides which ``ha_search`` degradation
fragments a no-tools lane is allowed to report: the two structural
YAML-scene classifications pass, every failure shape must be rejected. The
check is a pure sync function, so it belongs here rather than in the e2e
suite, where it re-ran identically on all ten lanes.

The module is loaded by file path: its imports are stdlib-only, so this
collects everywhere without importing the e2e package (and its Docker-shaped
conftest). The YAML-scene fragment the lanes see comes from a search on a
real ``hass``.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools

SCENE_FRAGMENTS_PATH = (
    Path(__file__).resolve().parents[1] / "e2e" / "utilities" / "scene_fragments.py"
)


def _load_scene_fragments() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "e2e_scene_fragments", SCENE_FRAGMENTS_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


scene_fragments = _load_scene_fragments()
is_structural_scene_fragment = scene_fragments.is_structural_scene_fragment

# Explicit + concatenation, not adjacent literals: CodeQL's
# implicit-string-concatenation-in-list check reads the latter as a
# possible missing comma.
SPOOFING_FAILED_FETCH = (
    "1 scene(s) not scanned (per-id fetch raised a non-404 error; e.g. "
    + "HTTP 500 while reading YAML-defined scenes) — their match status "
    + "is unknown; this result is not exhaustive."
)
TIMEOUT_FRAGMENT = (
    "2 scene(s) not scanned (per-id fetch timed out after 8.0s while 5 "
    + "fetches ran concurrently ...) — their match status is unknown"
)
# The production literal from ``_apply_scene_partial_flag``'s
# ``registry_failed`` branch (src/ha_mcp/tools/smart_search/_scenes.py), so the
# rejection is pinned to the string users actually see, not a paraphrase.
REGISTRY_FAILED_FRAGMENT = (
    "Entity-registry fetch failed; integration-platform filter "
    + "unavailable, attempted all scenes (false-positive failures "
    + "expected for integration-managed scenes). The registry is "
    + "also where a scene's storage key comes from, so the returned "
    + "`scene_id` values fall back to the entity-id slug and will "
    + "not resolve for a scene that was renamed in the UI."
)
YAML_STRUCTURAL = (
    "1 scene(s) not scanned (per-id config endpoint returned 404 — these "
    + "are likely YAML-defined scenes that the /config/scene/config REST "
    + "endpoint does not expose) — their match status is unknown; this "
    + "result is not exhaustive."
)
ATTRIBUTE_ONLY = (
    "1 integration-managed scenes are scored by attribute only (no per-id fetch)."
)


def test_matcher_rejects_failure_fragments():
    """A failure fragment whose sample mentions a YAML-defined scene, a
    timeout, and the registry fallback must all be rejected."""
    rejected = [
        SPOOFING_FAILED_FETCH,
        TIMEOUT_FRAGMENT,
        REGISTRY_FAILED_FRAGMENT,
        "unrelated warning that mentions scored by attribute only in passing",
    ]
    assert not [t for t in rejected if is_structural_scene_fragment(t)]


def test_matcher_accepts_the_two_structural_shapes():
    accepted = [YAML_STRUCTURAL, ATTRIBUTE_ONLY]
    assert all(is_structural_scene_fragment(t) for t in accepted)


async def test_matcher_accepts_the_yaml_scene_fragment_ha_search_reports(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The fragment a search reports for a YAML-defined scene, which Core's
    per-id config endpoint answers 404 for, is the structural one: pinned to
    the wording users see, not to a copy of it."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(
        hass,
        "scene",
        {"scene": [{"id": "sc0", "name": "Sc0", "entities": {"light.x": "on"}}]},
    )

    response = await SmartSearchTools(client=core_client).deep_search(
        "movie", ["scene"]
    )

    scene_fragment = response["partial_reason"].split(" ; ")[0]
    assert scene_fragment.startswith("1 scene(s) not scanned")
    assert is_structural_scene_fragment(scene_fragment)
