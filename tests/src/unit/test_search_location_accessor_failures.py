"""Search diagnostics for registry accessors that fail with or without a filter."""

from types import SimpleNamespace
from typing import Any

import pytest

from .test_component_ws_search import (
    FakeArea,
    FakeDevice,
    FakeFloor,
    FakeHass,
    FakeRegEntry,
    FakeState,
    make_view,
    wsapi,
)


def _search(
    monkeypatch: pytest.MonkeyPatch, view: Any, area_filter: str | None = "Kitchen"
) -> dict[str, Any]:
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    return wsapi._do_search(
        FakeHass(states=[FakeState("light.kitchen", "on")]),
        {"search_types": ["entity"], "area_filter": area_filter},
    )


@pytest.mark.parametrize("area_filter", ["Kitchen", None])
@pytest.mark.parametrize(
    ("registry_name", "method_name"),
    [
        ("entity", "async_get"),
        ("area", "async_get_area"),
        ("floor", "async_get_floor"),
    ],
)
def test_search_reports_failed_registry_lookup(
    monkeypatch: pytest.MonkeyPatch,
    registry_name: str,
    method_name: str,
    area_filter: str | None,
) -> None:
    view = make_view(
        entity={"light.kitchen": FakeRegEntry("light.kitchen", area_id="kitchen")},
        areas=[FakeArea("kitchen", "Kitchen", "ground")],
        floors=[FakeFloor("ground", "Ground floor")],
    )

    def broken(_key: str) -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(getattr(view, registry_name), method_name, broken)
    result = _search(monkeypatch, view, area_filter)

    assert result["partial"] is True
    assert registry_name in result["partial_reason"]
    diagnostic = "location" if area_filter else "entity"
    assert result["diagnostics"][f"{diagnostic}_registries_unavailable"] == 1


@pytest.mark.parametrize("broken", [False, True])
def test_older_mapping_like_device_registry(
    monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    device = FakeDevice("kitchen", area_id="kitchen")
    view = make_view(
        entity={"light.kitchen": FakeRegEntry("light.kitchen", device_id="kitchen")},
        areas=[FakeArea("kitchen", "Kitchen")],
    )

    class MappingLike:
        def values(self) -> list[Any]:
            if broken:
                raise RuntimeError("device collection unavailable")
            return [device]

        def __iter__(self) -> Any:
            return iter(["kitchen"])

    view.device = SimpleNamespace(devices=MappingLike())
    result = _search(monkeypatch, view)
    assert result["partial"] is broken
    assert result["entity_total_matches"] == (0 if broken else 1)


@pytest.mark.parametrize("area_filter", ["Kitchen", None])
def test_missing_registry_entries_are_not_accessor_failures(
    monkeypatch: pytest.MonkeyPatch, area_filter: str | None
) -> None:
    view = make_view(areas=[FakeArea("kitchen", "Kitchen")])
    result = _search(monkeypatch, view, area_filter)
    assert result["partial"] is False
    assert result["entity_total_matches"] == (0 if area_filter else 1)


@pytest.mark.parametrize("area_filter", ["Kitchen", None])
def test_search_reports_failed_device_collection(
    monkeypatch: pytest.MonkeyPatch, area_filter: str | None
) -> None:
    view = make_view(
        entity={"light.kitchen": FakeRegEntry("light.kitchen", device_id="kitchen")},
        areas=[FakeArea("kitchen", "Kitchen", "ground")],
        floors=[FakeFloor("ground", "Ground floor")],
        devices=[FakeDevice("kitchen", area_id="kitchen")],
    )

    class BrokenCollection:
        def __iter__(self) -> Any:
            raise RuntimeError("device collection unavailable")

    view.device.devices = BrokenCollection()
    result = _search(monkeypatch, view, area_filter)

    assert result["entity_total_matches"] == (0 if area_filter else 1)
    assert result["partial"] is True
    assert "device" in result["partial_reason"]
    diagnostic = "location" if area_filter else "entity"
    assert result["diagnostics"][f"{diagnostic}_registries_unavailable"] == 1
