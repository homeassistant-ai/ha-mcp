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
