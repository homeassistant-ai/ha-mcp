"""Unit tests for reference_validator.

Covers the extraction walker (pure, sync), the registry index builders,
the check layer, and the end-to-end async runner with a mock client.
Regression coverage for #940: a hallucinated
``notify.mobile_app_andrew_phone`` must produce a warning, not a
silent success.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.reference_validator import (
    build_entity_set,
    build_service_index,
    check_refs,
    extract_refs,
    validate_config_references,
)

# ---------------------------------------------------------------------------
# extract_refs — pure walker
# ---------------------------------------------------------------------------


class TestExtractRefs:
    def test_empty_config(self):
        result = extract_refs({})
        assert result["refs"] == []
        assert result["unvalidated_templates"] == 0
        assert result["blueprint_skipped"] is False

    def test_flat_service_call(self):
        config = {
            "alias": "t",
            "trigger": [{"platform": "time", "at": "07:00:00"}],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}
            ],
        }
        result = extract_refs(config)
        kinds = sorted((r["kind"], r["value"]) for r in result["refs"])
        assert kinds == [("entity", "light.kitchen"), ("service", "light.turn_on")]

    def test_hallucinated_service_is_extracted(self):
        """#940 reproduction: the bogus notify service must be extracted."""
        config = {
            "alias": "battery_alert",
            "trigger": [
                {
                    "platform": "numeric_state",
                    "entity_id": "sensor.grid_status_battery_percentage",
                    "below": 50,
                }
            ],
            "action": [{"service": "notify.mobile_app_andrew_phone"}],
        }
        result = extract_refs(config)
        services = [r for r in result["refs"] if r["kind"] == "service"]
        entities = [r for r in result["refs"] if r["kind"] == "entity"]
        assert len(services) == 1
        assert services[0]["value"] == "notify.mobile_app_andrew_phone"
        assert len(entities) == 1
        assert entities[0]["value"] == "sensor.grid_status_battery_percentage"

    def test_action_alias_for_service_key(self):
        """HA accepts both `service:` and `action:` as the call key."""
        config = {"action": [{"action": "light.turn_on"}]}
        result = extract_refs(config)
        assert [r["value"] for r in result["refs"]] == ["light.turn_on"]

    def test_entity_id_as_list(self):
        config = {
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": ["light.a", "light.b", "light.c"]},
                }
            ]
        }
        result = extract_refs(config)
        entities = sorted(r["value"] for r in result["refs"] if r["kind"] == "entity")
        assert entities == ["light.a", "light.b", "light.c"]

    def test_entity_id_in_trigger(self):
        config = {
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion"}],
            "action": [],
        }
        result = extract_refs(config)
        assert any(
            r["kind"] == "entity" and r["value"] == "binary_sensor.motion"
            for r in result["refs"]
        )

    def test_nested_choose_action(self):
        config = {
            "action": [
                {
                    "choose": [
                        {
                            "conditions": [
                                {
                                    "condition": "state",
                                    "entity_id": "sun.sun",
                                    "state": "above_horizon",
                                }
                            ],
                            "sequence": [
                                {
                                    "service": "cover.open_cover",
                                    "target": {"entity_id": "cover.garage"},
                                }
                            ],
                        }
                    ],
                    "default": [
                        {
                            "service": "light.turn_off",
                            "target": {"entity_id": "light.porch"},
                        }
                    ],
                }
            ]
        }
        result = extract_refs(config)
        service_values = {r["value"] for r in result["refs"] if r["kind"] == "service"}
        entity_values = {r["value"] for r in result["refs"] if r["kind"] == "entity"}
        assert service_values == {"cover.open_cover", "light.turn_off"}
        assert entity_values == {"sun.sun", "cover.garage", "light.porch"}

    def test_repeat_and_parallel(self):
        config = {
            "action": [
                {
                    "repeat": {
                        "count": 3,
                        "sequence": [
                            {
                                "service": "light.toggle",
                                "target": {"entity_id": "light.a"},
                            }
                        ],
                    }
                },
                {
                    "parallel": [
                        {
                            "service": "switch.turn_on",
                            "target": {"entity_id": "switch.x"},
                        },
                        {
                            "service": "switch.turn_on",
                            "target": {"entity_id": "switch.y"},
                        },
                    ]
                },
            ]
        }
        result = extract_refs(config)
        entities = sorted(r["value"] for r in result["refs"] if r["kind"] == "entity")
        assert entities == ["light.a", "switch.x", "switch.y"]

    def test_template_service_is_skipped_and_counted(self):
        config = {"action": [{"service": "{{ 'light.turn_on' }}"}]}
        result = extract_refs(config)
        assert result["refs"] == []
        assert result["unvalidated_templates"] == 1

    def test_template_entity_is_skipped_and_counted(self):
        config = {
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": "{{ states('input_text.target') }}"},
                }
            ]
        }
        result = extract_refs(config)
        services = [r for r in result["refs"] if r["kind"] == "service"]
        assert len(services) == 1
        entities = [r for r in result["refs"] if r["kind"] == "entity"]
        assert entities == []
        assert result["unvalidated_templates"] == 1

    def test_template_in_entity_list_only_skips_that_entry(self):
        config = {
            "action": [
                {
                    "service": "light.turn_on",
                    "target": {
                        "entity_id": ["light.kitchen", "{{ var }}", "light.hallway"]
                    },
                }
            ]
        }
        result = extract_refs(config)
        entities = sorted(r["value"] for r in result["refs"] if r["kind"] == "entity")
        assert entities == ["light.hallway", "light.kitchen"]
        assert result["unvalidated_templates"] == 1

    def test_blueprint_short_circuits(self):
        config = {
            "alias": "blueprint auto",
            "use_blueprint": {
                "path": "homeassistant/motion_light.yaml",
                "input": {
                    "motion_entity": "binary_sensor.motion",
                    "light_target": {"entity_id": "light.hallway"},
                },
            },
        }
        result = extract_refs(config)
        assert result["refs"] == []
        assert result["blueprint_skipped"] is True
        assert result["unvalidated_templates"] == 0

    def test_path_tracking(self):
        config = {
            "action": [
                {"service": "light.turn_on"},
                {"service": "fan.turn_on", "target": {"entity_id": "fan.ceiling"}},
            ]
        }
        result = extract_refs(config)
        paths = {r["path"] for r in result["refs"]}
        assert "action[0].service" in paths
        assert "action[1].service" in paths
        assert "action[1].target.entity_id" in paths

    def test_non_string_service_values_ignored(self):
        """A non-string service value shouldn't crash the walker."""
        config = {"action": [{"service": 42, "data": {"entity_id": None}}]}
        result = extract_refs(config)
        assert result["refs"] == []


# ---------------------------------------------------------------------------
# build_service_index / build_entity_set
# ---------------------------------------------------------------------------


class TestRegistryIndices:
    def test_build_service_index_shape(self):
        payload = [
            {
                "domain": "light",
                "services": {"turn_on": {}, "turn_off": {}, "toggle": {}},
            },
            {
                "domain": "notify",
                "services": {
                    "persistent_notification": {},
                    "mobile_app_real_phone": {},
                },
            },
        ]
        index = build_service_index(payload)
        assert index["light"] == {"turn_on", "turn_off", "toggle"}
        assert "mobile_app_real_phone" in index["notify"]

    def test_build_service_index_ignores_malformed(self):
        payload = [
            {"domain": "light", "services": {"turn_on": {}}},
            "garbage",
            {"domain": None, "services": {}},
            {"domain": "switch"},  # missing services
        ]
        index = build_service_index(payload)
        assert index == {"light": {"turn_on"}}

    def test_build_service_index_non_list(self):
        assert build_service_index(None) == {}
        assert build_service_index({}) == {}

    def test_build_entity_set(self):
        payload = [
            {"entity_id": "light.kitchen", "state": "on"},
            {"entity_id": "sensor.temp", "state": "21.5"},
            {"entity_id": None},
            "bad_entry",
        ]
        entities = build_entity_set(payload)
        assert entities == {"light.kitchen", "sensor.temp"}


# ---------------------------------------------------------------------------
# check_refs
# ---------------------------------------------------------------------------


class TestCheckRefs:
    @pytest.fixture
    def service_index(self) -> dict[str, set[str]]:
        return {
            "light": {"turn_on", "turn_off"},
            "notify": {"persistent_notification"},
        }

    @pytest.fixture
    def entity_set(self) -> set[str]:
        return {"light.kitchen", "sensor.temp", "binary_sensor.motion"}

    def test_valid_service_no_warning(self, service_index, entity_set):
        refs = [
            {"path": "action[0].service", "value": "light.turn_on", "kind": "service"}
        ]
        assert check_refs(refs, service_index, entity_set) == []

    def test_missing_service_warned(self, service_index, entity_set):
        refs = [
            {
                "path": "action[0].service",
                "value": "notify.mobile_app_andrew_phone",
                "kind": "service",
            }
        ]
        warnings = check_refs(refs, service_index, entity_set)
        assert len(warnings) == 1
        assert warnings[0]["kind"] == "service"
        assert warnings[0]["value"] == "notify.mobile_app_andrew_phone"
        assert "not found" in warnings[0]["reason"]

    def test_bogus_domain_warned(self, service_index, entity_set):
        refs = [{"path": "p", "value": "bogus.thing", "kind": "service"}]
        warnings = check_refs(refs, service_index, entity_set)
        assert len(warnings) == 1

    def test_service_without_dot_warned(self, service_index, entity_set):
        refs = [{"path": "p", "value": "not_a_service", "kind": "service"}]
        warnings = check_refs(refs, service_index, entity_set)
        assert len(warnings) == 1

    def test_valid_entity_no_warning(self, service_index, entity_set):
        refs = [
            {"path": "trigger[0].entity_id", "value": "light.kitchen", "kind": "entity"}
        ]
        assert check_refs(refs, service_index, entity_set) == []

    def test_missing_entity_warned(self, service_index, entity_set):
        refs = [
            {"path": "trigger[0].entity_id", "value": "sensor.ghost", "kind": "entity"}
        ]
        warnings = check_refs(refs, service_index, entity_set)
        assert len(warnings) == 1
        assert warnings[0]["kind"] == "entity"
        assert warnings[0]["value"] == "sensor.ghost"

    def test_mixed_refs_partial_failure(self, service_index, entity_set):
        refs = [
            {"path": "action[0].service", "value": "light.turn_on", "kind": "service"},
            {
                "path": "action[0].target.entity_id",
                "value": "light.ghost",
                "kind": "entity",
            },
            {
                "path": "trigger[0].entity_id",
                "value": "binary_sensor.motion",
                "kind": "entity",
            },
        ]
        warnings = check_refs(refs, service_index, entity_set)
        assert len(warnings) == 1
        assert warnings[0]["value"] == "light.ghost"


# ---------------------------------------------------------------------------
# validate_config_references — async end-to-end
# ---------------------------------------------------------------------------


def _mock_client(services_payload: Any, states_payload: Any) -> Any:
    client = AsyncMock()
    client.get_services = AsyncMock(return_value=services_payload)
    client.get_states = AsyncMock(return_value=states_payload)
    return client


class TestValidateConfigReferences:
    @pytest.mark.anyio
    async def test_940_reproduction_produces_warning(self):
        """The exact scenario from #940 must yield a service warning."""
        config = {
            "alias": "battery_alert",
            "trigger": [
                {
                    "platform": "numeric_state",
                    "entity_id": "sensor.grid_status_battery_percentage",
                    "below": 50,
                }
            ],
            "action": [{"service": "notify.mobile_app_andrew_phone"}],
        }
        client = _mock_client(
            services_payload=[
                {"domain": "notify", "services": {"persistent_notification": {}}},
            ],
            states_payload=[
                {"entity_id": "sensor.grid_status_battery_percentage", "state": "45"},
            ],
        )
        result = await validate_config_references(client, config)
        assert result["blueprint_skipped"] is False
        assert result["unvalidated_templates"] == 0
        warnings = result["warnings"]
        assert len(warnings) == 1
        assert warnings[0]["kind"] == "service"
        assert warnings[0]["value"] == "notify.mobile_app_andrew_phone"

    @pytest.mark.anyio
    async def test_all_valid_no_warnings(self):
        config = {
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}
            ]
        }
        client = _mock_client(
            services_payload=[{"domain": "light", "services": {"turn_on": {}}}],
            states_payload=[{"entity_id": "light.kitchen", "state": "off"}],
        )
        result = await validate_config_references(client, config)
        assert result["warnings"] == []

    @pytest.mark.anyio
    async def test_blueprint_short_circuits_no_fetch(self):
        """Blueprint configs must not even hit the registries."""
        config = {
            "alias": "bp",
            "use_blueprint": {
                "path": "homeassistant/motion_light.yaml",
                "input": {"motion_entity": "binary_sensor.motion"},
            },
        }
        client = _mock_client(services_payload=[], states_payload=[])
        result = await validate_config_references(client, config)
        assert result["blueprint_skipped"] is True
        assert result["warnings"] == []
        client.get_services.assert_not_called()
        client.get_states.assert_not_called()

    @pytest.mark.anyio
    async def test_registry_fetch_failure_is_swallowed(self, caplog):
        """A broken client must never break the automation write path."""
        config = {"action": [{"service": "light.turn_on"}]}
        client = AsyncMock()
        client.get_services = AsyncMock(side_effect=RuntimeError("boom"))
        client.get_states = AsyncMock(return_value=[])
        with caplog.at_level("ERROR"):
            result = await validate_config_references(client, config)
        assert result["warnings"] == []
        assert any("validator" in rec.message.lower() for rec in caplog.records)

    @pytest.mark.anyio
    async def test_empty_config_no_fetch(self):
        """No refs → no registry fetches, no warnings."""
        client = _mock_client(services_payload=[], states_payload=[])
        result = await validate_config_references(client, {"alias": "empty"})
        assert result["warnings"] == []
        client.get_services.assert_not_called()
        client.get_states.assert_not_called()

    @pytest.mark.anyio
    async def test_templates_counted_not_warned(self):
        config = {
            "action": [
                {"service": "{{ 'light.turn_on' }}"},
                {"service": "light.turn_off", "target": {"entity_id": "light.real"}},
            ]
        }
        client = _mock_client(
            services_payload=[{"domain": "light", "services": {"turn_off": {}}}],
            states_payload=[{"entity_id": "light.real", "state": "on"}],
        )
        result = await validate_config_references(client, config)
        assert result["warnings"] == []
        assert result["unvalidated_templates"] == 1
