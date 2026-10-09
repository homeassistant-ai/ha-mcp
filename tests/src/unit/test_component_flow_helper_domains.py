"""The component's flow helpers are the helper flows the running Core lists."""

import json
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from .test_component_ws_search import (
    FakeConfig,
    FakeConfigEntry,
    FakeHass,
    empty_view,  # noqa: F401 - pytest fixture
    wsapi,
)

pytestmark = pytest.mark.usefixtures("empty_view")

_MARKER = "flowdomainoption5521"
_CORE_MARKER = "coreoverrideoption7713"


def _search(entry: FakeConfigEntry, query: str, **extra: object) -> dict:
    return wsapi._do_search(
        FakeHass(config_entries=[entry]),
        {"query": query, "search_types": ["helper"], "include_config": True},
        **extra,
    )


def _flow(res: dict) -> list[dict]:
    return [h for h in res["helpers"] if h["kind"] == "flow"]


def _hass(tmp_path, entries: list[FakeConfigEntry] | None = None) -> FakeHass:
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
    [("_helpers_list_prep", {}), ("_search_prep", {"search_types": ["helper"]})],
)
async def test_prep_asks_core_for_its_helper_flows(
    monkeypatch: pytest.MonkeyPatch, tmp_path, prep: str, msg: dict
) -> None:
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
async def test_prep_without_flow_helpers_skips_the_helper_flow_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path, prep: str, msg: dict
) -> None:
    loader = _loader(monkeypatch, set())
    extra = await getattr(wsapi, prep)(_hass(tmp_path), msg)
    assert "flow_domains" not in extra
    loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_helpers_list_withholds_only_custom_only_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Through the real pre-step: a custom-only helper is listed without its
    options, while a Core helper domain keeps them even if a custom integration
    overrides it."""
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
    options = {h["helper_type"]: h["options"] for h in _flow(res)}
    assert options == {"template": {"k": _CORE_MARKER}, "my_helper": None}
    assert "my_helper" in res["covered_types"]
    assert _MARKER not in json.dumps(res)
    only = wsapi._do_helpers_list(hass, {"helper_types": ["my_helper"]}, **extra)
    assert [h["helper_type"] for h in _flow(only)] == ["my_helper"]


@pytest.mark.asyncio
async def test_search_matches_custom_only_helpers_on_title_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _loader(monkeypatch, {"template", "my_helper"})
    entry = FakeConfigEntry(
        "my_helper", title="Custom Helper", options={"api_key": _MARKER}
    )
    hass = _hass(tmp_path, [entry])
    msg = {"search_types": ["helper"], "include_config": True}
    extra = await wsapi._search_prep(hass, msg)
    by_title = wsapi._do_search(hass, {**msg, "query": "custom helper"}, **extra)
    assert [(h["helper_type"], h["options"]) for h in _flow(by_title)] == [
        ("my_helper", None)
    ]
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
    "degraded,include_config,warned",
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_search_warns_of_a_degraded_scrub_when_it_emits_options(
    degraded: bool, include_config: bool, warned: bool
) -> None:
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
    assert (wsapi.SCRUB_DEGRADED_WARNING in res.get("warnings", [])) is warned


@pytest.mark.asyncio
async def test_helper_search_prep_reports_an_unreadable_secrets_file(
    tmp_path,
) -> None:
    (tmp_path / "secrets.yaml").write_text("key: [unclosed\n", encoding="utf-8")
    extra = await wsapi._search_prep(_hass(tmp_path), {"search_types": ["helper"]})
    assert extra["secret_scrub_degraded"] is True
