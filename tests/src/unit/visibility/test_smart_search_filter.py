"""Visibility hard-exclude wired into the smart_search entity helpers."""

from ha_mcp.tools.smart_search._entities import EntitySearchMixin


def test_filter_hidden_entities_hard_excludes_visibility_set():
    entities = [{"entity_id": "sensor.a"}, {"entity_id": "light.b"}]
    survivor_ids, survivor_states = EntitySearchMixin._filter_hidden_entities(
        entities,
        registry_slim={},
        include_hidden=True,
        visibility_hidden={"sensor.a"},
    )
    assert survivor_ids == ["light.b"]
    assert [s["entity_id"] for s in survivor_states] == ["light.b"]


def test_filter_hidden_entities_empty_set_keeps_all():
    entities = [{"entity_id": "sensor.a"}, {"entity_id": "light.b"}]
    survivor_ids, _ = EntitySearchMixin._filter_hidden_entities(
        entities, registry_slim={}, include_hidden=True, visibility_hidden=set()
    )
    assert survivor_ids == ["sensor.a", "light.b"]


def test_resolve_entity_areas_hard_excludes_visibility_set():
    entity_reg_map = {
        "sensor.a": {"area_id": "kitchen", "device_id": None, "hidden_by": None},
        "light.b": {"area_id": "kitchen", "device_id": None, "hidden_by": None},
    }
    resolved, _hidden = EntitySearchMixin._resolve_entity_areas(
        entity_reg_map,
        device_area_map={},
        include_hidden=True,
        visibility_hidden={"sensor.a"},
    )
    assert resolved == {"light.b": "kitchen"}


def test_resolve_entity_areas_empty_set_keeps_all():
    entity_reg_map = {
        "sensor.a": {"area_id": "kitchen", "device_id": None, "hidden_by": None},
    }
    resolved, _hidden = EntitySearchMixin._resolve_entity_areas(
        entity_reg_map,
        device_area_map={},
        include_hidden=True,
        visibility_hidden=set(),
    )
    assert resolved == {"sensor.a": "kitchen"}
