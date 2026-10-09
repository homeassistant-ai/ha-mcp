"""The component's flow helpers are Core's ``FLOWS["helper"]``.

The conftest stub carries Core 2026.10's list: ``otp`` is a helper there, and
``bayesian`` (manifest ``integration_type: "service"``) is filed under
``FLOWS["integration"]``.
"""

import json

import pytest

from .test_component_ws_search import FakeConfigEntry, FakeHass, wsapi


@pytest.fixture(autouse=True)
def _empty_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView()
    )


@pytest.mark.parametrize("domain,listed", [("otp", True), ("bayesian", False)])
def test_listing_covers_the_helpers_core_lists(domain: str, listed: bool) -> None:
    entry = FakeConfigEntry(domain, title="Helper", entry_id="e1")
    res = wsapi._do_helpers_list(FakeHass(config_entries=[entry]), {})
    flow = [h for h in res["helpers"] if h["kind"] == "flow"]
    assert bool(flow) is listed
    assert (domain in res["covered_types"]) is listed


@pytest.mark.parametrize("domain,listed", [("otp", True), ("bayesian", False)])
def test_search_indexes_only_the_helpers_core_lists(domain: str, listed: bool) -> None:
    """An entry outside Core's helper list must not reach search, options included."""
    marker = "flowdomainoption5521"
    entry = FakeConfigEntry(
        domain, title="Sun Helper", options={"note": marker}, entry_id="e1"
    )
    res = wsapi._do_search(
        FakeHass(config_entries=[entry]),
        {"query": "sun helper", "search_types": ["helper"], "include_config": True},
    )
    flow = [h for h in res["helpers"] if h["kind"] == "flow"]
    assert bool(flow) is listed
    assert (marker in json.dumps(res)) is listed
