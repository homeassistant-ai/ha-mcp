"""Search degradation with and without the component's shared surface."""

import logging

import pytest

from ..utilities.assertions import MCPAssertions
from ..utilities.scene_fragments import is_structural_scene_fragment, is_yaml_scene_gap
from ..utilities.topology import component_surface_available

logger = logging.getLogger(__name__)


_YAML_ITEM_GAP = "not scanned (per-id config endpoint returned 404"


def _assert_only_yaml_scene_degradation(data: dict) -> None:
    """Every degradation the response reports is the YAML-scene one (#2292).

    ``partial_reason`` is one string joining its fragments with `` ; ``, so it
    is split back apart — a response whose reason mixes the structural
    YAML-scene fragment with a "per-id fetch raised" one must fail, not pass on
    the strength of the acceptable half. The classification itself lives in
    ``utilities/scene_fragments.py``, pinned by
    ``tests/src/unit/test_scene_fragment_matcher.py``.
    """
    fragments = list(data.get("warnings") or [])
    fragments += [
        part.strip()
        for part in (data.get("partial_reason") or "").split(" ; ")
        if part.strip()
    ]
    # The seeded package automation and script (custom_packages/
    # e2e_yaml_defined.yaml) are the same structural gap for their domains: the
    # legacy path reads bodies from Core's config API, which answers 404 for them.
    yaml_item_gaps = [text for text in fragments if _YAML_ITEM_GAP in text]
    if data.get("partial") is True:
        # Only a YAML gap explains an omission; the attribute-only note
        # describes scenes that were still scored.
        assert yaml_item_gaps or any(is_yaml_scene_gap(text) for text in fragments), (
            f"A partial result must name a YAML gap: {data}"
        )
    offenders = [
        text
        for text in fragments
        if text not in yaml_item_gaps and not is_structural_scene_fragment(text)
    ]
    assert not offenders, (
        "Only the structural YAML fragments are acceptable without a "
        f"component surface; got {offenders}"
    )


@pytest.mark.component_topology
@pytest.mark.asyncio
async def test_search_entities_successful_fuzzy_search_no_warning(mcp_client):
    """Test that successful fuzzy search returns no warning or partial flag.

    Issue #214: Normal fuzzy search should work without fallback indicators.
    """
    logger.info("Testing successful fuzzy search has no fallback indicators")

    async with MCPAssertions(mcp_client) as mcp:
        data = await mcp.call_tool_success(
            "ha_search",
            {"query": "light", "limit": 5, "exact_match": False},
        )

    assert data.get("success") is True
    assert data.get("search_type") == "fuzzy_search"
    if component_surface_available():
        # Normal fuzzy search should NOT have warning or partial flag
        assert not data.get("warnings"), f"Unexpected warnings: {data.get('warnings')}"
        assert "partial" not in data or data.get("partial") is not True
    else:
        # #2292: with no active component entry the search runs on the legacy
        # REST path, whose /config/scene/config endpoint serves only storage
        # scenes — a YAML-defined scene carrying an ``id:`` is indistinguishable
        # in the registry and then 404s. That gap is structural, not a fetch
        # outage, and ``smart_search/_scenes.py`` classifies the 404 as
        # ``yaml_skipped`` so the wording says so. This branch pins that wording
        # as the ONLY degradation this topology may produce: a fetch failure or
        # any other non-404 cause still has to fail the test.
        _assert_only_yaml_scene_degradation(data)
    # Strong matches should not include suggestions
    assert "suggestions" not in data, "Strong matches should not include suggestions"

    logger.info("Fuzzy search succeeded without fallback indicators")
