"""Search degradation with and without the component's shared surface."""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success
from ..utilities.scene_fragments import is_structural_scene_fragment
from ..utilities.topology import component_surface_available

logger = logging.getLogger(__name__)


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
    offenders = [text for text in fragments if not is_structural_scene_fragment(text)]
    assert not offenders, (
        "Only the structural YAML-scene fragments are acceptable without a "
        f"component surface; got {offenders}"
    )


@pytest.mark.component_topology
@pytest.mark.asyncio
async def test_search_entities_successful_fuzzy_search_no_warning(mcp_client):
    """Test that successful fuzzy search returns no warning or partial flag.

    Issue #214: Normal fuzzy search should work without fallback indicators.
    """
    logger.info("Testing successful fuzzy search has no fallback indicators")

    result = await mcp_client.call_tool(
        "ha_search",
        {"query": "light", "limit": 5, "exact_match": False},
    )
    data = assert_mcp_success(result, "Fuzzy search success")

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
