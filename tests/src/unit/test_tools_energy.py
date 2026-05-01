"""Unit tests for EnergyTools — covers all three modes plus error paths.

End-to-end tests would require a live Home Assistant with an Energy Dashboard
configured and an admin token; mocking ``send_websocket_message`` keeps these
hermetic while still exercising every branch of the state machine.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_energy import (
    EnergyTools,
    _flatten_validation_errors,
    _shape_check,
)
from ha_mcp.utils.config_hash import compute_config_hash

# -----------------------------------------------------------------------------
# Fixtures / helpers
# -----------------------------------------------------------------------------


@pytest.fixture
def tools():
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return EnergyTools(client)


def _sample_prefs() -> dict:
    return {
        "energy_sources": [
            {
                "type": "grid",
                "stat_energy_from": "sensor.grid_import",
                "stat_energy_to": None,
                "stat_cost": None,
                "entity_energy_price": None,
                "number_energy_price": None,
                "cost_adjustment_day": 0,
                "entity_energy_price_export": None,
                "number_energy_price_export": None,
                "stat_compensation": None,
            }
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.fridge_energy"},
        ],
        "device_consumption_water": [],
    }


def _empty_validate_result() -> dict:
    return {
        "energy_sources": [[]],
        "device_consumption": [[]],
        "device_consumption_water": [],
    }


# -----------------------------------------------------------------------------
# _flatten_validation_errors
# -----------------------------------------------------------------------------


class TestFlattenValidationErrors:
    def test_all_empty_returns_empty_list(self):
        assert _flatten_validation_errors(_empty_validate_result()) == []

    def test_non_dict_input_returns_empty(self):
        assert _flatten_validation_errors(None) == []
        assert _flatten_validation_errors("string") == []
        assert _flatten_validation_errors([]) == []

    def test_list_of_strings_per_entry(self):
        raw = {
            "energy_sources": [["stat not found"], []],
            "device_consumption": [],
            "device_consumption_water": [],
        }
        errors = _flatten_validation_errors(raw)
        assert errors == [{"path": "energy_sources[0]", "message": "stat not found"}]

    def test_dict_per_entry_with_field_paths(self):
        raw = {
            "energy_sources": [],
            "device_consumption": [
                {"stat_consumption": ["unit mismatch", "stat missing"]}
            ],
            "device_consumption_water": [],
        }
        errors = _flatten_validation_errors(raw)
        assert {
            "path": "device_consumption[0].stat_consumption",
            "message": "unit mismatch",
        } in errors
        assert {
            "path": "device_consumption[0].stat_consumption",
            "message": "stat missing",
        } in errors
        assert len(errors) == 2


# -----------------------------------------------------------------------------
# _shape_check
# -----------------------------------------------------------------------------


class TestShapeCheck:
    def test_valid_config(self):
        assert _shape_check(_sample_prefs()) == []

    def test_non_dict_config(self):
        errors = _shape_check([])  # type: ignore[arg-type]
        assert errors == [{"path": "config", "message": "must be a dict"}]

    def test_top_level_not_a_list(self):
        errors = _shape_check({"device_consumption": "not a list"})
        assert {"path": "device_consumption", "message": "must be a list"} in errors

    def test_energy_source_missing_type(self):
        errors = _shape_check(
            {
                "energy_sources": [{"stat_energy_from": "sensor.x"}],
            }
        )
        assert any("type" in e["message"] for e in errors)

    def test_device_consumption_missing_stat_consumption(self):
        errors = _shape_check(
            {
                "device_consumption": [{"name": "anonymous"}],
            }
        )
        assert any("stat_consumption" in e["message"] for e in errors)

    def test_entry_not_a_dict(self):
        errors = _shape_check({"device_consumption": ["not a dict"]})
        assert any("must be a dict" in e["message"] for e in errors)

    def test_unknown_top_level_keys_ignored(self):
        # Unknown keys are harmless at shape-check level — they'll simply not
        # be forwarded to save_prefs by the tool.
        assert _shape_check({"something_else": 42}) == []


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="get"
# -----------------------------------------------------------------------------


class TestGetPrefs:
    async def test_happy_path_returns_config_and_hash(self, tools):
        prefs = _sample_prefs()
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": prefs,
        }

        result = await tools.ha_manage_energy_prefs(mode="get")

        assert result["success"] is True
        assert result["mode"] == "get"
        assert result["config"] == prefs
        assert result["config_hash"] == compute_config_hash(prefs)

    async def test_ws_failure_raises_tool_error(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": False,
            "error": "something broke",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(mode="get")

        err = json.loads(str(exc_info.value))
        assert err["success"] is False
        assert "SERVICE_CALL_FAILED" in json.dumps(err)

    async def test_no_prefs_error_returns_empty_default(self, tools):
        """Fresh HA without a configured Energy Dashboard returns
        ERR_NOT_FOUND 'No prefs' — the tool must map that to an empty
        default so the get/set workflow works uniformly."""
        tools._client.send_websocket_message.return_value = {
            "success": False,
            "error": "Command failed: No prefs",
        }

        result = await tools.ha_manage_energy_prefs(mode="get")

        assert result["success"] is True
        assert result["mode"] == "get"
        assert result["config"] == {
            "energy_sources": [],
            "device_consumption": [],
            "device_consumption_water": [],
        }
        assert result["config_hash"] == compute_config_hash(result["config"])
        assert "note" in result
        assert "never been configured" in result["note"]


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="set" parameter validation
# -----------------------------------------------------------------------------


class TestSetParameterValidation:
    async def test_missing_config_raises(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(mode="set")
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_MISSING_PARAMETER" in json.dumps(err)
        assert "config" in json.dumps(err).lower()

    async def test_missing_hash_without_dry_run_raises(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=_sample_prefs(),
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_MISSING_PARAMETER" in json.dumps(err)
        assert "config_hash" in json.dumps(err).lower()

    async def test_missing_hash_with_dry_run_ok(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": _empty_validate_result(),
        }
        # dry_run=True skips the hash requirement
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=_sample_prefs(),
            dry_run=True,
        )
        assert result["success"] is True
        assert result["dry_run"] is True


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="set" dry_run
# -----------------------------------------------------------------------------


class TestDryRun:
    async def test_valid_config_success(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": _empty_validate_result(),
        }
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=_sample_prefs(),
            dry_run=True,
        )
        assert result["success"] is True
        assert result["shape_errors"] == []
        assert result["current_state_validation_errors"] == []

    async def test_shape_errors_surfaced(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": _empty_validate_result(),
        }
        bad_config = {"energy_sources": [{"stat_energy_from": "sensor.x"}]}
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=bad_config,
            dry_run=True,
        )
        assert result["success"] is False
        assert len(result["shape_errors"]) > 0
        assert any("type" in e["message"] for e in result["shape_errors"])

    async def test_shape_errors_energy_sources_enum_and_conditional(self, tools):
        """G1b + G1c coverage:
        - invalid type reports as '.type' path with enum message
        - solar/battery/gas without stat_energy_from reports required
        - grid without stat_energy_from is valid (HA core schema: Optional)
        """
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": _empty_validate_result(),
        }

        # Invalid type
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config={"energy_sources": [{"type": "wind", "stat_energy_from": "s.x"}]},
            dry_run=True,
        )
        assert result["success"] is False
        assert any(
            e["path"].endswith(".type") and "invalid type 'wind'" in e["message"]
            for e in result["shape_errors"]
        )

        # Solar without stat_energy_from
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config={"energy_sources": [{"type": "solar"}]},
            dry_run=True,
        )
        assert result["success"] is False
        assert any(
            "solar entries require 'stat_energy_from'" in e["message"]
            for e in result["shape_errors"]
        )

        # Battery without stat_energy_from
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config={"energy_sources": [{"type": "battery"}]},
            dry_run=True,
        )
        assert result["success"] is False
        assert any(
            "battery entries require 'stat_energy_from'" in e["message"]
            for e in result["shape_errors"]
        )

        # Gas without stat_energy_from
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config={"energy_sources": [{"type": "gas"}]},
            dry_run=True,
        )
        assert result["success"] is False
        assert any(
            "gas entries require 'stat_energy_from'" in e["message"]
            for e in result["shape_errors"]
        )

        # Grid without stat_energy_from (valid per HA core schema)
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config={"energy_sources": [{"type": "grid"}]},
            dry_run=True,
        )
        # Grid is valid: no shape errors about stat_energy_from on grid.
        # (Other fields may still be complained about, but stat_energy_from
        # must NOT appear in shape_errors for type=grid.)
        grid_stat_errors = [
            e
            for e in result.get("shape_errors", [])
            if "stat_energy_from" in e["message"]
        ]
        assert grid_stat_errors == []

    async def test_current_state_errors_surfaced_separately(self, tools):
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": {
                "energy_sources": [["stat missing"]],
                "device_consumption": [],
                "device_consumption_water": [],
            },
        }
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=_sample_prefs(),
            dry_run=True,
        )
        assert result["success"] is True  # shape is fine
        assert result["shape_errors"] == []
        assert len(result["current_state_validation_errors"]) == 1

    async def test_validate_failure_surfaced_as_warning(self, tools, caplog):
        """If energy/validate returns success=false in dry_run, the caller
        sees partial/warning rather than a silent empty current_state_errors
        list."""
        import logging

        tools._client.send_websocket_message.return_value = {
            "success": False,
            "error": "websocket timeout",
        }
        with caplog.at_level(logging.WARNING, logger="ha_mcp.tools.tools_energy"):
            result = await tools.ha_manage_energy_prefs(
                mode="set",
                config=_sample_prefs(),
                dry_run=True,
            )
        assert result["success"] is True  # shape is fine
        assert result["current_state_validation_errors"] == []
        assert result["partial"] is True
        assert "websocket timeout" in result["warning"]
        assert any(
            "energy/validate (current state) failed" in rec.message
            for rec in caplog.records
        )


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="set" write path
# -----------------------------------------------------------------------------


class TestSetPrefs:
    async def test_shape_error_rejected_before_read(self, tools):
        bad_config = {"device_consumption": [{"name": "no-stat"}]}
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=bad_config,
                config_hash="abc",
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_FAILED" in json.dumps(err)
        # No WS call should have happened
        tools._client.send_websocket_message.assert_not_called()

    async def test_hash_mismatch_rejects_and_does_not_save(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]
        stale_hash = "deadbeefcafefade"
        assert stale_hash != compute_config_hash(current_prefs)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=current_prefs,
                config_hash=stale_hash,
            )
        err = json.loads(str(exc_info.value))
        assert "modified since last read" in json.dumps(err).lower()
        assert "RESOURCE_LOCKED" in json.dumps(err)
        # Only ONE WS call (the fresh read); no save
        assert tools._client.send_websocket_message.call_count == 1

    async def test_happy_path_writes_and_validates(self, tools):
        current_prefs = _sample_prefs()
        hash_ = compute_config_hash(current_prefs)
        new_config = {
            **current_prefs,
            "device_consumption": [
                {"stat_consumption": "sensor.fridge_energy"},
                {"stat_consumption": "sensor.tv_energy"},
            ],
        }

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},  # 1. fresh read
            {"success": True, "result": None},  # 2. save_prefs
            {
                "success": True,
                "result": _empty_validate_result(),
            },  # 3. post-save validate
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=new_config,
            config_hash=hash_,
        )
        assert result["success"] is True
        assert result["mode"] == "set"
        assert "config_hash" in result
        assert "post_save_validation_errors" not in result  # none reported
        assert tools._client.send_websocket_message.call_count == 3

    async def test_save_fails_raises(self, tools):
        current_prefs = _sample_prefs()
        hash_ = compute_config_hash(current_prefs)

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": False, "error": "unauthorized"},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=current_prefs,
                config_hash=hash_,
            )
        err = json.loads(str(exc_info.value))
        assert "SERVICE_CALL_FAILED" in json.dumps(err)

    async def test_post_save_validation_errors_surfaced_as_warning(self, tools):
        current_prefs = _sample_prefs()
        hash_ = compute_config_hash(current_prefs)

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {
                "success": True,
                "result": {
                    "energy_sources": [["stat not found"]],
                    "device_consumption": [],
                    "device_consumption_water": [],
                },
            },
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=current_prefs,
            config_hash=hash_,
        )
        assert result["success"] is True  # save succeeded
        assert "post_save_validation_errors" in result
        assert len(result["post_save_validation_errors"]) == 1
        assert "warning" in result

    async def test_post_save_validation_failure_non_fatal(self, tools):
        """If the post-save validate itself fails, the save still succeeded.

        The exception-branch sets post_save_validate_error, which surfaces
        as partial/warning in the response.
        """
        current_prefs = _sample_prefs()
        hash_ = compute_config_hash(current_prefs)

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            Exception("validate blew up"),
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=current_prefs,
            config_hash=hash_,
        )
        assert result["success"] is True
        assert "post_save_validation_errors" not in result
        assert result["partial"] is True
        assert "validate blew up" in result["warning"]

    async def test_post_save_validate_failure_surfaced_as_warning(self, tools, caplog):
        """If post-save energy/validate returns success=false, the caller sees
        partial/warning rather than a silent empty post_save_errors list."""
        import logging

        current_prefs = _sample_prefs()
        hash_ = compute_config_hash(current_prefs)

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": False, "error": "validate endpoint missing"},
        ]

        with caplog.at_level(logging.WARNING, logger="ha_mcp.tools.tools_energy"):
            result = await tools.ha_manage_energy_prefs(
                mode="set",
                config=current_prefs,
                config_hash=hash_,
            )
        assert result["success"] is True
        assert "post_save_validation_errors" not in result
        assert result["partial"] is True
        assert "validate endpoint missing" in result["warning"]
        assert any(
            "energy/validate (post-save) failed" in rec.message
            for rec in caplog.records
        )

    async def test_save_payload_contains_only_submitted_keys(self, tools):
        """Full-replace only affects keys explicitly in the submitted payload."""
        current_prefs = _sample_prefs()
        hash_ = compute_config_hash(current_prefs)
        # Agent only wants to touch device_consumption
        partial_config = {"device_consumption": [{"stat_consumption": "sensor.new"}]}

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        # Hash must be fresh of the current prefs, not the partial config —
        # that's the agent's responsibility. For this test we use the right hash.
        await tools.ha_manage_energy_prefs(
            mode="set",
            config=partial_config,
            config_hash=hash_,
        )
        save_call = tools._client.send_websocket_message.call_args_list[1]
        save_payload = save_call.args[0]
        assert save_payload["type"] == "energy/save_prefs"
        assert "device_consumption" in save_payload
        # energy_sources and device_consumption_water must NOT be in the save
        # payload — their absence preserves the existing server state.
        assert "energy_sources" not in save_payload
        assert "device_consumption_water" not in save_payload

    async def test_set_on_fresh_install_with_default_hash_succeeds(self, tools):
        """On a fresh HA install, get_prefs yields 'No prefs'. An agent that
        holds the hash of the empty default (e.g. from a prior mode='get'
        call that already normalised the No-prefs case) must be able to
        save through."""
        empty_default = {
            "energy_sources": [],
            "device_consumption": [],
            "device_consumption_water": [],
        }
        default_hash = compute_config_hash(empty_default)
        new_config = {
            "device_consumption": [{"stat_consumption": "sensor.first_device"}],
        }

        tools._client.send_websocket_message.side_effect = [
            {"success": False, "error": "Command failed: No prefs"},  # 1. get
            {"success": True, "result": None},  # 2. save
            {
                "success": True,
                "result": _empty_validate_result(),
            },  # 3. post-save validate
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=new_config,
            config_hash=default_hash,
        )
        assert result["success"] is True
        assert "config_hash" in result

    async def test_set_on_fresh_install_with_wrong_hash_rejects(self, tools):
        """Even on a fresh install, the hash check protects the write path —
        a stale hash against the default-empty baseline still fails."""
        tools._client.send_websocket_message.side_effect = [
            {"success": False, "error": "Command failed: No prefs"},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config={"device_consumption": [{"stat_consumption": "sensor.x"}]},
                config_hash="deadbeefcafefade",
            )
        err = json.loads(str(exc_info.value))
        assert "modified since last read" in json.dumps(err).lower()
        assert tools._client.send_websocket_message.call_count == 1


# -----------------------------------------------------------------------------
# Tool wiring
# -----------------------------------------------------------------------------


class TestRegistration:
    def test_register_function_exists_and_has_expected_signature(self):
        import inspect

        from ha_mcp.tools.tools_energy import register_energy_tools

        sig = inspect.signature(register_energy_tools)
        params = list(sig.parameters.keys())
        assert params[0] == "mcp"
        assert params[1] == "client"
        # kwargs-accepting for registry compatibility
        assert any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="add_device"
# -----------------------------------------------------------------------------


class TestAddDevice:
    async def test_missing_stat_consumption_raises(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(mode="add_device")
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_MISSING_PARAMETER" in json.dumps(err)
        assert "stat_consumption" in json.dumps(err).lower()

    async def test_happy_path_appends_to_device_consumption(self, tools):
        current_prefs = _sample_prefs()  # has fridge_energy
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},  # 1. initial _get_prefs
            {"success": True, "result": current_prefs},  # 2. _set_prefs fresh re-read
            {"success": True, "result": None},  # 3. save_prefs
            {"success": True, "result": _empty_validate_result()},  # 4. post-save
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.tv_energy",
            name="TV",
        )

        assert result["success"] is True
        assert result["mode"] == "add_device"
        assert result["target_key"] == "device_consumption"
        assert result["new_count"] == 2  # fridge + tv

        # The save payload must be a partial — only device_consumption.
        save_call = tools._client.send_websocket_message.call_args_list[2]
        save_payload = save_call.args[0]
        assert save_payload["type"] == "energy/save_prefs"
        assert "device_consumption" in save_payload
        # Per-key full-replace semantics — other keys must NOT be in the save payload.
        assert "energy_sources" not in save_payload
        assert "device_consumption_water" not in save_payload
        # New entry shape
        new_devices = save_payload["device_consumption"]
        assert len(new_devices) == 2
        assert new_devices[1]["stat_consumption"] == "sensor.tv_energy"
        assert new_devices[1]["name"] == "TV"

    async def test_water_flag_targets_water_list(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.water_meter",
            water=True,
        )

        assert result["target_key"] == "device_consumption_water"
        save_payload = tools._client.send_websocket_message.call_args_list[2].args[0]
        assert "device_consumption_water" in save_payload
        assert "device_consumption" not in save_payload

    async def test_duplicate_raises_already_exists(self, tools):
        current_prefs = _sample_prefs()  # has sensor.fridge_energy
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_device",
                stat_consumption="sensor.fridge_energy",  # already there
            )
        err = json.loads(str(exc_info.value))
        assert "RESOURCE_ALREADY_EXISTS" in json.dumps(err)
        assert "sensor.fridge_energy" in json.dumps(err)

    async def test_dry_run_does_not_write(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},  # only one call expected
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.tv_energy",
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["new_count"] == 2
        assert result["current_count"] == 1
        # Exactly one WS call (the read); no save_prefs.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_dry_run_raises_on_duplicate(self, tools):
        """dry_run does not bypass mutator validation: adding a duplicate
        raises RESOURCE_ALREADY_EXISTS even with dry_run=True (the mutator
        runs before the dry-run check)."""
        current_prefs = _sample_prefs()  # has sensor.fridge_energy
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_device",
                stat_consumption="sensor.fridge_energy",  # already present
                dry_run=True,
            )
        err = json.loads(str(exc_info.value))
        assert "RESOURCE_ALREADY_EXISTS" in json.dumps(err)
        assert "sensor.fridge_energy" in json.dumps(err)
        # Only the initial read; no save_prefs.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_fresh_install_no_prefs_starts_empty(self, tools):
        # First call returns "No prefs"; tool maps to default empty.
        tools._client.send_websocket_message.side_effect = [
            {"success": False, "error": "Command failed: No prefs"},  # initial get
            {"success": False, "error": "Command failed: No prefs"},  # set re-read
            {"success": True, "result": None},  # save
            {"success": True, "result": _empty_validate_result()},  # post-save
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.fridge_energy",
        )

        assert result["success"] is True
        assert result["new_count"] == 1


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="remove_device"
# -----------------------------------------------------------------------------


class TestRemoveDevice:
    async def test_missing_stat_consumption_raises(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(mode="remove_device")
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_MISSING_PARAMETER" in json.dumps(err)

    async def test_happy_path_removes_entry(self, tools):
        current_prefs = _sample_prefs()  # has sensor.fridge_energy
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="remove_device",
            stat_consumption="sensor.fridge_energy",
        )

        assert result["success"] is True
        assert result["new_count"] == 0
        save_payload = tools._client.send_websocket_message.call_args_list[2].args[0]
        assert save_payload["device_consumption"] == []

    async def test_not_found_raises(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="remove_device",
                stat_consumption="sensor.does_not_exist",
            )
        err = json.loads(str(exc_info.value))
        assert "RESOURCE_NOT_FOUND" in json.dumps(err)
        assert "sensor.does_not_exist" in json.dumps(err)

    async def test_dry_run_does_not_write(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]
        result = await tools.ha_manage_energy_prefs(
            mode="remove_device",
            stat_consumption="sensor.fridge_energy",
            dry_run=True,
        )
        assert result["dry_run"] is True
        assert result["new_count"] == 0
        assert tools._client.send_websocket_message.call_count == 1

    async def test_dry_run_raises_on_missing(self, tools):
        """dry_run does not bypass mutator validation: removing a non-
        existent device raises RESOURCE_NOT_FOUND even with dry_run=True."""
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="remove_device",
                stat_consumption="sensor.does_not_exist",
                dry_run=True,
            )
        err = json.loads(str(exc_info.value))
        assert "RESOURCE_NOT_FOUND" in json.dumps(err)
        assert "sensor.does_not_exist" in json.dumps(err)
        assert tools._client.send_websocket_message.call_count == 1


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="add_source"
# -----------------------------------------------------------------------------


class TestAddSource:
    async def test_missing_source_raises(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(mode="add_source")
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_MISSING_PARAMETER" in json.dumps(err)
        assert "source" in json.dumps(err).lower()

    async def test_invalid_type_raises_validation_failed(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_source",
                source={"type": "wind"},  # not in valid_types
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_FAILED" in json.dumps(err)

    async def test_solar_missing_stat_energy_from_raises(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_source",
                source={"type": "solar"},  # solar requires stat_energy_from
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_FAILED" in json.dumps(err)
        assert "stat_energy_from" in json.dumps(err)

    async def test_grid_happy_path(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        new_grid = {"type": "grid", "stat_energy_from": "sensor.grid_2"}
        result = await tools.ha_manage_energy_prefs(
            mode="add_source",
            source=new_grid,
        )

        assert result["success"] is True
        assert result["target_key"] == "energy_sources"
        save_payload = tools._client.send_websocket_message.call_args_list[2].args[0]
        assert "energy_sources" in save_payload
        assert "device_consumption" not in save_payload
        assert save_payload["energy_sources"][-1] == new_grid

    async def test_dry_run_does_not_write(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]
        result = await tools.ha_manage_energy_prefs(
            mode="add_source",
            source={"type": "solar", "stat_energy_from": "sensor.solar_2"},
            dry_run=True,
        )
        assert result["dry_run"] is True
        assert tools._client.send_websocket_message.call_count == 1


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — convenience-mode hash-conflict retry
# -----------------------------------------------------------------------------


class TestConvenienceRetryOnHashConflict:
    async def test_add_device_retries_once_on_hash_conflict(self, tools):
        """First write attempt sees a fresh-read mismatch (concurrent
        modification), the retry loop reads fresh and succeeds."""
        prefs_v1 = _sample_prefs()
        prefs_v2 = {  # someone else added a device between our read and write
            **prefs_v1,
            "device_consumption": [
                *prefs_v1["device_consumption"],
                {"stat_consumption": "sensor.intruder"},
            ],
        }

        tools._client.send_websocket_message.side_effect = [
            # Attempt 1: initial get → set re-read (mismatch → RESOURCE_LOCKED)
            {"success": True, "result": prefs_v1},  # initial get
            {"success": True, "result": prefs_v2},  # set re-read sees v2 → hash diff
            # Attempt 2: fresh get → set re-read (consistent) → save → validate
            {"success": True, "result": prefs_v2},  # initial get (retry)
            {"success": True, "result": prefs_v2},  # set re-read
            {"success": True, "result": None},  # save_prefs
            {"success": True, "result": _empty_validate_result()},  # post-save
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.tv_energy",
        )
        assert result["success"] is True
        # Retry consumed all 6 mocked calls.
        assert tools._client.send_websocket_message.call_count == 6

    async def test_retry_exhaustion_raises_resource_locked(self, tools):
        """Two consecutive hash conflicts: the retry's set re-read also
        sees a fresh external write. _mutate_atomic exits the loop via
        `raise last_error`, surfacing the RESOURCE_LOCKED ToolError."""
        prefs_v1 = _sample_prefs()
        prefs_v2 = {  # external write before the first set
            **prefs_v1,
            "device_consumption": [
                *prefs_v1["device_consumption"],
                {"stat_consumption": "sensor.intruder_a"},
            ],
        }
        prefs_v3 = {  # second external write before the retry's set
            **prefs_v2,
            "device_consumption": [
                *prefs_v2["device_consumption"],
                {"stat_consumption": "sensor.intruder_b"},
            ],
        }

        tools._client.send_websocket_message.side_effect = [
            # Attempt 1: get v1 → set re-read sees v2 → RESOURCE_LOCKED
            {"success": True, "result": prefs_v1},
            {"success": True, "result": prefs_v2},
            # Attempt 2 (retry): get v2 → set re-read sees v3 → RESOURCE_LOCKED again
            {"success": True, "result": prefs_v2},
            {"success": True, "result": prefs_v3},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_device",
                stat_consumption="sensor.tv_energy",
            )
        err = json.loads(str(exc_info.value))
        assert "RESOURCE_LOCKED" in json.dumps(err)
        # 4 calls total (2 attempts × 2 calls each), no save_prefs ever.
        assert tools._client.send_websocket_message.call_count == 4
