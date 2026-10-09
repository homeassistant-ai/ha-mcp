"""The component's flow helpers are the helper flows the running Core lists."""

import json
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.search import component as search_component

from ._component_routing_helpers import patch_ws
from .test_component_search_visibility_contract import _real_search_ws
from .test_component_ws_search import (
    FakeConfig,
    FakeConfigEntry,
    FakeHass,
    empty_view,  # noqa: F401 - pytest fixture
    wsapi,
)
from .test_ha_search_component_routing import RoutingClient, _build_ha_search

pytestmark = pytest.mark.usefixtures("empty_view")

_MARKER = "flowdomainoption5521"
_CORE_MARKER = "coreoverrideoption7713"
_UNREADABLE_SECRETS = "key: [unclosed\n"


def _search(entry: FakeConfigEntry, query: str, **extra: object) -> dict:
    return wsapi._do_search(
        FakeHass(config_entries=[entry]),
        {"query": query, "search_types": ["helper"], "include_config": True},
        **extra,
    )


def _flow(res: dict) -> list[dict]:
    return [h for h in res["helpers"] if h["kind"] == "flow"]


def _hass(tmp_path: Path, entries: list[FakeConfigEntry] | None = None) -> FakeHass:
    hass = FakeHass(config_entries=entries or [])
    hass.config = FakeConfig(tmp_path)
    return hass


def _loader(monkeypatch: pytest.MonkeyPatch, flows: set[str]) -> AsyncMock:
    loader = AsyncMock(return_value=flows)
    monkeypatch.setattr(wsapi, "async_get_config_flows", loader)
    return loader


@pytest.mark.parametrize("domain,listed", [("otp", True), ("bayesian", False)])
def test_listing_covers_the_helpers_core_lists(domain: str, listed: bool) -> None:
    entry = FakeConfigEntry(domain, title="Helper", entry_id="e1")
    res = wsapi._do_helpers_list(FakeHass(config_entries=[entry]), {})
    flow = _flow(res)
    assert bool(flow) is listed
    if listed:
        assert flow[0]["helper_type"] == domain
    assert (domain in res["covered_types"]) is listed


@pytest.mark.parametrize("domain,listed", [("otp", True), ("bayesian", False)])
def test_search_indexes_only_the_helpers_core_lists(domain: str, listed: bool) -> None:
    """A Core helper (otp) is searched with its options; an entry outside Core's
    helper list (bayesian) reaches neither the results nor the options."""
    entry = FakeConfigEntry(
        domain, title="Sun Helper", options={"note": _MARKER}, entry_id="e1"
    )
    res = _search(entry, "sun helper")
    assert bool(_flow(res)) is listed
    assert (_MARKER in json.dumps(res)) is listed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prep,msg",
    [
        ("_helpers_list_prep", {}),
        ("_search_prep", {"search_types": ["helper"]}),
        ("_search_prep", {}),
    ],
)
async def test_custom_helper_integrations_reach_listing_and_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prep: str, msg: dict
) -> None:
    """Both commands take Core's helper flows, custom helper integrations
    included, and withhold only the custom-only domains' options."""
    loader = _loader(monkeypatch, {"template", "my_helper"})
    hass = _hass(tmp_path)
    extra = await getattr(wsapi, prep)(hass, msg)
    loader.assert_awaited_once_with(hass, "helper")
    assert extra["flow_domains"] == frozenset({"template", "my_helper"})
    # template is in Core's own list, so a custom override of it is not withheld.
    assert extra["custom_domains"] == frozenset({"my_helper"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prep,msg",
    [
        ("_helpers_list_prep", {"include_flow_helpers": False}),
        ("_search_prep", {"search_types": ["automation"]}),
    ],
)
async def test_requests_without_flow_helpers_do_not_wait_on_the_loader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prep: str, msg: dict
) -> None:
    loader = _loader(monkeypatch, set())
    extra = await getattr(wsapi, prep)(_hass(tmp_path), msg)
    assert "flow_domains" not in extra
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_loader_read_lists_core_helpers_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A loader failure must not fail the command (the server would report the
    component as missing); it falls back to Core's own list, withholds nothing
    custom, and reports the gap."""
    monkeypatch.setattr(
        wsapi, "async_get_config_flows", AsyncMock(side_effect=RuntimeError("boom"))
    )
    entry = FakeConfigEntry("template", title="Sun Helper", options={"k": 1})
    hass = _hass(tmp_path, [entry])
    extra = await wsapi._helpers_list_prep(hass, {})
    assert extra["flow_domains"] == wsapi.FLOW_HELPER_DOMAINS
    assert extra["custom_domains"] == frozenset()
    listing = wsapi._do_helpers_list(hass, {}, **extra)
    assert [h["helper_type"] for h in _flow(listing)] == ["template"]
    assert listing["helper_flows_degraded"] is True
    msg = {"search_types": ["helper"], "query": "sun helper"}
    found = wsapi._do_search(hass, msg, **await wsapi._search_prep(hass, msg))
    assert wsapi.HELPER_FLOWS_DEGRADED_WARNING in found["warnings"]


@pytest.mark.asyncio
async def test_helpers_list_withholds_only_custom_only_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through the real pre-step: a custom-only helper is listed without its
    options and marked as withheld, while a Core helper domain keeps them even if
    a custom integration overrides it."""
    _loader(monkeypatch, {"template", "my_helper"})
    hass = _hass(
        tmp_path,
        [
            FakeConfigEntry("template", options={"k": _CORE_MARKER}, entry_id="e1"),
            FakeConfigEntry("my_helper", options={"api_key": _MARKER}, entry_id="e2"),
        ],
    )
    extra = await wsapi._helpers_list_prep(hass, {})
    res = wsapi._do_helpers_list(hass, {}, **extra)
    flow = {h["helper_type"]: h for h in _flow(res)}
    assert flow["template"]["options"] == {"k": _CORE_MARKER}
    assert "options_withheld" not in flow["template"]
    assert flow["my_helper"]["options"] is None
    assert flow["my_helper"]["options_withheld"] == "custom_integration"
    assert "my_helper" in res["covered_types"]
    assert _MARKER not in json.dumps(res)
    only = wsapi._do_helpers_list(hass, {"helper_types": ["my_helper"]}, **extra)
    assert [h["helper_type"] for h in _flow(only)] == ["my_helper"]


@pytest.mark.asyncio
async def test_search_matches_custom_only_helpers_on_title_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _loader(monkeypatch, {"template", "my_helper"})
    entry = FakeConfigEntry(
        "my_helper", title="Custom Helper", options={"api_key": _MARKER}
    )
    hass = _hass(tmp_path, [entry])
    msg = {"search_types": ["helper"], "include_config": True}
    extra = await wsapi._search_prep(hass, msg)
    by_title = wsapi._do_search(hass, {**msg, "query": "custom helper"}, **extra)
    (record,) = _flow(by_title)
    assert (record["helper_type"], record["options"]) == ("my_helper", None)
    assert record["options_withheld"] == "custom_integration"
    assert _MARKER not in json.dumps(by_title)
    by_option = wsapi._do_search(hass, {**msg, "query": _MARKER}, **extra)
    assert _flow(by_option) == []


def test_search_scrubs_resolved_secrets_from_flow_helper_options() -> None:
    entry = FakeConfigEntry(
        "template",
        title="Sun Helper",
        options=MappingProxyType(
            {"state": "{{ 1 }}", "nested": MappingProxyType({"url": "sekret"})}
        ),
        entry_id="e1",
    )
    res = _search(entry, "sun helper", secret_values=frozenset({"sekret"}))
    assert _flow(res)[0]["options"] == {
        "state": "{{ 1 }}",
        "nested": {"url": "**redacted**"},
    }


@pytest.mark.parametrize(
    "degraded,include_config,warning",
    [
        (True, True, "SCRUB_DEGRADED_WARNING"),
        (True, False, "SCRUB_DEGRADED_MATCH_WARNING"),
        (False, True, None),
    ],
)
def test_search_warns_when_options_were_handled_without_the_scrub(
    degraded: bool, include_config: bool, warning: str | None
) -> None:
    """Emitted options and the match corpus both go unscrubbed when secrets.yaml
    is unreadable; each case gets its own warning."""
    entry = FakeConfigEntry("template", title="Sun Helper", entry_id="e1")
    res = wsapi._do_search(
        FakeHass(config_entries=[entry]),
        {
            "query": "sun helper",
            "search_types": ["helper"],
            "include_config": include_config,
        },
        secret_scrub_degraded=degraded,
    )
    expected = [getattr(wsapi, warning)] if warning else []
    assert res.get("warnings", []) == expected


@pytest.mark.asyncio
async def test_helper_search_prep_reports_an_unreadable_secrets_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "secrets.yaml").write_text(_UNREADABLE_SECRETS, encoding="utf-8")
    extra = await wsapi._search_prep(_hass(tmp_path), {"search_types": ["helper"]})
    assert extra["secret_scrub_degraded"] is True


@pytest.mark.asyncio
async def test_ha_search_shows_the_scrub_warning_and_the_withheld_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through ha_search: the component's warning and the
    options_withheld marker survive the server's response shaping."""
    _loader(monkeypatch, {"template", "my_helper"})
    (tmp_path / "secrets.yaml").write_text(_UNREADABLE_SECRETS, encoding="utf-8")
    hass = _hass(
        tmp_path,
        [FakeConfigEntry("my_helper", title="Sun Helper", options={"k": _MARKER})],
    )
    ha_search = _build_ha_search(RoutingClient())
    with patch_ws(_real_search_ws(hass), search_component):
        resp = await ha_search(
            query="sun helper", search_types=["helper"], include_config=True
        )
    assert wsapi.SCRUB_DEGRADED_WARNING in resp["warnings"]
    (record,) = resp["helpers"]
    assert record["options_withheld"] == "custom_integration"
    assert record["config"] is None
