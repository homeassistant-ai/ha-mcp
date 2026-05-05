"""Unit tests for EnergyTools — covers all three modes plus error paths.

End-to-end tests would require a live Home Assistant with an Energy Dashboard
configured and an admin token; mocking ``send_websocket_message`` keeps these
hermetic while still exercising every branch of the state machine.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_energy import (
    _PREFS_TOP_LEVEL_KEYS,
    EnergyTools,
    _compute_per_key_hashes,
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
# _shape_check(validate_only=...) — issue #1086 (scope per-entry check)
# -----------------------------------------------------------------------------


class TestShapeCheckValidateOnly:
    """``validate_only`` lets convenience-mode write paths skip re-validating
    pre-existing entries that HA already accepted. ``None`` is the original
    full-validation contract; a dict scopes per-key/per-index; an empty dict
    skips the per-entry pass entirely."""

    def test_none_validates_everything(self):
        # Default behaviour preserved — invalid entry surfaces.
        bad = {"device_consumption": [{"name": "no-stat"}]}
        assert _shape_check(bad, validate_only=None)
        assert _shape_check(bad)  # default arg, identical contract

    def test_empty_dict_skips_all_per_entry_checks(self):
        bad = {"device_consumption": [{"name": "no-stat"}]}
        assert _shape_check(bad, validate_only={}) == []

    def test_listed_key_with_empty_index_set_skips_per_entry_but_keeps_structural(self):
        # ``{key: set()}`` is "key listed, no per-entry indices" — distinct
        # from ``{}`` (skip everything). The list-shape structural check
        # still fires for that key; per-entry checks are skipped. This is
        # the shape ``_remove_*`` mutators pass through ``_appended_tail_indices``
        # after a shrink (no new entries to validate, surviving entries
        # already passed HA validation).
        bad = {
            "device_consumption": [
                {"name": "no-stat-bad-1"},
                {"name": "no-stat-bad-2"},
            ],
        }
        assert (
            _shape_check(bad, validate_only={"device_consumption": set()})
            == []
        )

    def test_unlisted_key_is_skipped(self):
        # Bad entry under device_consumption — but validate_only only asks
        # for energy_sources, so device_consumption is skipped entirely.
        config = {
            "device_consumption": [{"name": "no-stat"}],
            "energy_sources": [{"type": "grid"}],
        }
        assert (
            _shape_check(
                config, validate_only={"energy_sources": {0}}
            )
            == []
        )

    def test_unlisted_indices_within_a_key_are_skipped(self):
        # device_consumption[0] is bad, [1] is good. validate_only={1} only
        # checks the good index — no errors surface.
        config = {
            "device_consumption": [
                {"name": "no-stat-bad"},
                {"stat_consumption": "sensor.good"},
            ],
        }
        assert _shape_check(config, validate_only={"device_consumption": {1}}) == []

    def test_listed_index_with_bad_entry_still_raises(self):
        # validate_only including a bad index still surfaces it.
        config = {
            "device_consumption": [
                {"stat_consumption": "sensor.good"},
                {"name": "no-stat-bad"},
            ],
        }
        errors = _shape_check(
            config, validate_only={"device_consumption": {1}}
        )
        assert any("stat_consumption" in e["message"] for e in errors)

    def test_structural_must_be_a_list_check_still_fires_for_listed_keys(self):
        # The "must be a list" structural check is independent of
        # validate_only's per-entry filter — caller cannot bypass it for a
        # key they explicitly listed.
        errors = _shape_check(
            {"device_consumption": "not a list"},
            validate_only={"device_consumption": {0}},
        )
        assert {"path": "device_consumption", "message": "must be a list"} in errors

    def test_structural_must_be_a_list_check_skipped_for_unlisted_keys(self):
        # …but a non-list value under an UNLISTED key is skipped, since
        # the caller did not ask for that key.
        assert (
            _shape_check(
                {"device_consumption": "not a list"},
                validate_only={"energy_sources": set()},
            )
            == []
        )


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

    async def test_response_includes_per_key_hashes(self, tools):
        """Per-key hashes for partial-update optimistic locking. Every
        canonical top-level key is present (even when its value is the
        empty list, mirroring _default_prefs)."""
        prefs = _sample_prefs()
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": prefs,
        }

        result = await tools.ha_manage_energy_prefs(mode="get")

        assert "config_hash_per_key" in result
        assert set(result["config_hash_per_key"]) == set(_PREFS_TOP_LEVEL_KEYS)
        # Each per-key hash is the full-blob hash of {key: prefs[key]} —
        # this disambiguates {energy_sources: []} from {device_consumption: []}
        # so an empty-list hash for one key never authorises a write to another.
        for key in _PREFS_TOP_LEVEL_KEYS:
            assert result["config_hash_per_key"][key] == compute_config_hash(
                {key: prefs[key]}
            )

    async def test_no_prefs_response_includes_per_key_hashes(self, tools):
        """Even on a fresh-install (No prefs) response, per-key hashes are
        populated against the empty default — so an agent can immediately
        chain a per-key set without a second round-trip."""
        tools._client.send_websocket_message.return_value = {
            "success": False,
            "error": "Command failed: No prefs",
        }

        result = await tools.ha_manage_energy_prefs(mode="get")

        assert "config_hash_per_key" in result
        assert set(result["config_hash_per_key"]) == set(_PREFS_TOP_LEVEL_KEYS)
        for key in _PREFS_TOP_LEVEL_KEYS:
            assert result["config_hash_per_key"][key] == compute_config_hash(
                {key: []}
            )


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
# ha_manage_energy_prefs — mode="set" with per-top-level-key config_hash
# -----------------------------------------------------------------------------


class TestSetPrefsPerKeyHash:
    """Per-key form of ``config_hash`` (issue #1049).

    The dict form locks each submitted top-level key independently, so an
    agent that only mutates ``device_consumption`` is not rejected when an
    unrelated key (``energy_sources``) was concurrently changed by another
    client. Set-equality between submitted ``config`` keys and dict keys
    is fail-closed; unknown keys (outside the canonical top-level set)
    are silently dropped on both sides, mirroring existing _shape_check
    semantics.
    """

    async def test_happy_path_partial_save_with_per_key_hash(self, tools):
        current_prefs = _sample_prefs()
        per_key = _compute_per_key_hashes(current_prefs)
        # Agent mutates only device_consumption.
        new_dc = [
            {"stat_consumption": "sensor.fridge_energy"},
            {"stat_consumption": "sensor.tv_energy"},
        ]
        partial_config = {"device_consumption": new_dc}

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=partial_config,
            config_hash={"device_consumption": per_key["device_consumption"]},
        )
        assert result["success"] is True
        assert result["mode"] == "set"
        # Save payload only carries the submitted top-level key.
        save_payload = tools._client.send_websocket_message.call_args_list[
            1
        ].args[0]
        assert save_payload["type"] == "energy/save_prefs"
        assert save_payload["device_consumption"] == new_dc
        assert "energy_sources" not in save_payload
        assert "device_consumption_water" not in save_payload

    async def test_per_key_mismatch_lists_offending_keys(self, tools):
        current_prefs = _sample_prefs()
        per_key = _compute_per_key_hashes(current_prefs)
        # Pretend the agent's view of device_consumption is stale; the
        # other two keys' hashes are still fresh.
        stale_dc_hash = "deadbeefcafefade"
        assert stale_dc_hash != per_key["device_consumption"]

        # Submit two keys, only one is stale.
        config = {
            "device_consumption": [{"stat_consumption": "sensor.new"}],
            "energy_sources": current_prefs["energy_sources"],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=config,
                config_hash={
                    "device_consumption": stale_dc_hash,
                    "energy_sources": per_key["energy_sources"],
                },
            )
        err = json.loads(str(exc_info.value))
        assert "RESOURCE_LOCKED" in json.dumps(err)
        # Only the stale key surfaces in mismatched_keys.
        assert err["mismatched_keys"] == ["device_consumption"]
        # No save WS call.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_per_key_mismatch_lists_all_offending_keys_sorted(self, tools):
        """Two stale keys must both appear in mismatched_keys, sorted —
        regression guard against `next(...)` / single-element shortcuts
        on the comprehension at the dict-branch's mismatch loop.
        """
        current_prefs = _sample_prefs()
        per_key = _compute_per_key_hashes(current_prefs)
        # Both device_consumption and energy_sources are stale; only
        # device_consumption_water hash is fresh (and not submitted).
        config = {
            "device_consumption": [{"stat_consumption": "sensor.fridge_energy"}],
            "energy_sources": current_prefs["energy_sources"],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=config,
                config_hash={
                    "device_consumption": "stale1deadbeefcafe",
                    "energy_sources": "stale2deadbeefcafe",
                },
            )
        err = json.loads(str(exc_info.value))
        assert "RESOURCE_LOCKED" in json.dumps(err)
        # Both stale keys surface, in lexicographic sorted order.
        assert err["mismatched_keys"] == [
            "device_consumption",
            "energy_sources",
        ]
        assert tools._client.send_websocket_message.call_count == 1

    async def test_missing_hash_for_submitted_key_raises_validation(self, tools):
        current_prefs = _sample_prefs()
        per_key = _compute_per_key_hashes(current_prefs)
        # Submit two keys, hash only one — fail closed.
        config = {
            "device_consumption": [{"stat_consumption": "sensor.x"}],
            "energy_sources": current_prefs["energy_sources"],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=config,
                config_hash={
                    "device_consumption": per_key["device_consumption"],
                },
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_FAILED" in json.dumps(err)
        assert err["missing_in_hash"] == ["energy_sources"]
        assert err["extra_in_hash"] == []
        # Hash check is the gate — no save happened.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_extra_hash_key_not_in_config_raises_validation(self, tools):
        current_prefs = _sample_prefs()
        per_key = _compute_per_key_hashes(current_prefs)
        # Submit only device_consumption but supply hashes for two keys.
        config = {"device_consumption": [{"stat_consumption": "sensor.x"}]}
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=config,
                config_hash={
                    "device_consumption": per_key["device_consumption"],
                    "energy_sources": per_key["energy_sources"],
                },
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_FAILED" in json.dumps(err)
        assert err["extra_in_hash"] == ["energy_sources"]
        assert err["missing_in_hash"] == []
        assert tools._client.send_websocket_message.call_count == 1

    async def test_unknown_top_level_keys_rejected(self, tools):
        """Unknown top-level keys (outside the canonical set) on either
        side are rejected with VALIDATION_FAILED. Closes the silent-no-op
        trap where typo'd keys on both sides would coincide as ∅ == ∅
        and slip through as a save with an empty payload.
        """
        current_prefs = _sample_prefs()
        per_key = _compute_per_key_hashes(current_prefs)
        config = {
            "device_consumption": [{"stat_consumption": "sensor.fridge_energy"}],
            "garbage_key": "ignored",
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=config,
                config_hash={
                    "device_consumption": per_key["device_consumption"],
                    "another_garbage_key": "also_ignored",
                },
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_FAILED" in json.dumps(err)
        assert err["invalid_config_keys"] == ["garbage_key"]
        assert err["invalid_hash_keys"] == ["another_garbage_key"]
        # Hash mismatch is the gate — no save WS call.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_all_typoed_keys_does_not_silently_succeed(self, tools):
        """Belt-and-braces: even if EVERY submitted key is a typo (so the
        old silent-drop logic would have left submitted_keys=∅ and
        coincided with hashed_keys=∅), the unknown-key guard rejects
        before set-equality runs. No no-op save reaches HA.
        """
        current_prefs = _sample_prefs()
        config_only_typos = {"devic_consumption": [{"stat_consumption": "x"}]}
        hash_only_typos = {"devic_consumption": "deadbeefcafefade"}
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=config_only_typos,
                config_hash=hash_only_typos,
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_FAILED" in json.dumps(err)
        assert err["invalid_config_keys"] == ["devic_consumption"]
        assert err["invalid_hash_keys"] == ["devic_consumption"]
        # No save attempted.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_empty_config_with_dict_hash_rejected(self, tools):
        """Empty 'config' under dict-form hash is rejected — even with
        valid (empty) hashed_keys the agent's intent is unclear, and the
        save endpoint would no-op silently.
        """
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config={},
                config_hash={},
            )
        err = json.loads(str(exc_info.value))
        assert "VALIDATION_FAILED" in json.dumps(err)
        assert "at least one top-level key" in json.dumps(err)
        assert tools._client.send_websocket_message.call_count == 1

    async def test_response_includes_fresh_per_key_hashes_after_write(
        self, tools
    ):
        """After a per-key write, the response carries an updated
        config_hash_per_key reflecting the new merged state. An agent can
        chain another per-key write without an intermediate mode='get'.
        """
        current_prefs = _sample_prefs()
        per_key = _compute_per_key_hashes(current_prefs)
        new_dc = [{"stat_consumption": "sensor.new"}]

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config={"device_consumption": new_dc},
            config_hash={"device_consumption": per_key["device_consumption"]},
        )
        assert "config_hash_per_key" in result
        # device_consumption hash reflects the new value; the other two
        # keys retain their prior hashes (full-replace only on submitted
        # keys).
        expected_after = {
            "energy_sources": current_prefs["energy_sources"],
            "device_consumption": new_dc,
            "device_consumption_water": current_prefs[
                "device_consumption_water"
            ],
        }
        assert result["config_hash_per_key"] == _compute_per_key_hashes(
            expected_after
        )
        # Backward-compat: full-blob config_hash is also still emitted.
        assert result["config_hash"] == compute_config_hash(expected_after)

    async def test_str_hash_path_unchanged(self, tools):
        """Backward compatibility: passing a str config_hash exercises the
        full-blob path exactly as before (unchanged contract).
        """
        current_prefs = _sample_prefs()
        full_hash = compute_config_hash(current_prefs)

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=current_prefs,
            config_hash=full_hash,
        )
        assert result["success"] is True
        assert result["mode"] == "set"

    async def test_dry_run_with_dict_form_skips_hash_check(self, tools):
        """``dry_run=True`` short-circuits before the hash branch entirely,
        so a dict-form ``config_hash`` (even one with stale or invalid
        entries) is silently accepted on dry runs. Documented in the
        tool docstring; pinned here so a future refactor cannot quietly
        start enforcing the dict shape under dry_run.
        """
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": _empty_validate_result(),
        }
        result = await tools.ha_manage_energy_prefs(
            mode="set",
            config=_sample_prefs(),
            # All three fields stale — would be RESOURCE_LOCKED under
            # a real-run, but dry_run skips the check.
            config_hash={
                "energy_sources": "deadbeefcafefade",
                "device_consumption": "deadbeefcafefade",
                "device_consumption_water": "deadbeefcafefade",
            },
            dry_run=True,
        )
        assert result["success"] is True
        assert result["dry_run"] is True


# -----------------------------------------------------------------------------
# Convenience-mode contract — never reaches dict-form hash branch (issue #1049)
# -----------------------------------------------------------------------------


class TestConvenienceModesPassStrHash:
    """``_mutate_atomic`` reads ``current["config_hash"]`` (always a ``str``
    from ``_get_prefs``) and forwards it to ``_set_prefs``. The dict-form
    code path is therefore unreachable from convenience modes. Pinning
    that contract guards against a future refactor that forwards
    ``config_hash_per_key`` instead — which would expose dict-branch
    semantics to a code path that pre-validates differently.
    """

    async def test_add_device_forwards_str_hash_to_set_prefs(self, tools, monkeypatch):
        from ha_mcp.tools.tools_energy import EnergyTools

        original_set_prefs = EnergyTools._set_prefs
        captured: dict[str, Any] = {}

        async def spy_set_prefs(self, config, config_hash, **kwargs):
            captured["config_hash_type"] = type(config_hash).__name__
            return await original_set_prefs(self, config, config_hash, **kwargs)

        monkeypatch.setattr(EnergyTools, "_set_prefs", spy_set_prefs)

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": _sample_prefs()},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]
        await tools.ha_manage_energy_prefs(
            mode="add_device", stat_consumption="sensor.tv_energy"
        )
        assert captured["config_hash_type"] == "str"

    async def test_remove_device_forwards_str_hash_to_set_prefs(
        self, tools, monkeypatch
    ):
        from ha_mcp.tools.tools_energy import EnergyTools

        original_set_prefs = EnergyTools._set_prefs
        captured: dict[str, Any] = {}

        async def spy_set_prefs(self, config, config_hash, **kwargs):
            captured["config_hash_type"] = type(config_hash).__name__
            return await original_set_prefs(self, config, config_hash, **kwargs)

        monkeypatch.setattr(EnergyTools, "_set_prefs", spy_set_prefs)

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": _sample_prefs()},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]
        await tools.ha_manage_energy_prefs(
            mode="remove_device", stat_consumption="sensor.fridge_energy"
        )
        assert captured["config_hash_type"] == "str"

    async def test_add_source_forwards_str_hash_to_set_prefs(
        self, tools, monkeypatch
    ):
        from ha_mcp.tools.tools_energy import EnergyTools

        original_set_prefs = EnergyTools._set_prefs
        captured: dict[str, Any] = {}

        async def spy_set_prefs(self, config, config_hash, **kwargs):
            captured["config_hash_type"] = type(config_hash).__name__
            return await original_set_prefs(self, config, config_hash, **kwargs)

        monkeypatch.setattr(EnergyTools, "_set_prefs", spy_set_prefs)

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": _sample_prefs()},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]
        await tools.ha_manage_energy_prefs(
            mode="add_source",
            source={
                "type": "solar",
                "stat_energy_from": "sensor.solar_in",
            },
        )
        assert captured["config_hash_type"] == "str"


# -----------------------------------------------------------------------------
# issue #1086 — convenience-mode writes do not re-validate pre-existing entries
# -----------------------------------------------------------------------------


class TestConvenienceModesPreExistingInvalid:
    """Regression for issue #1086. ``_set_prefs`` previously ran
    ``_shape_check`` over the full union ``existing + new``, so a
    pre-existing entry that fails the local schema would block an
    unrelated add/remove. The fix scopes the check to the appended tail
    via ``validate_only``: ``add_*`` validates only the new entry,
    ``remove_*`` validates nothing (the snapshot was already HA-valid).
    Reproduced here by feeding back a deliberately-invalid pre-existing
    entry from ``energy/get_prefs``.
    """

    async def test_add_device_succeeds_with_invalid_pre_existing(self, tools):
        invalid_prefs = {
            "energy_sources": [],
            "device_consumption": [{"name": "broken_no_stat"}],  # invalid
            "device_consumption_water": [],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": invalid_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device", stat_consumption="sensor.fridge_energy"
        )
        assert result["success"] is True
        # The pre-existing broken entry survives unchanged in the saved
        # payload — full-replace semantics on the top-level key.
        save_payload = tools._client.send_websocket_message.call_args_list[
            1
        ].args[0]
        assert save_payload["device_consumption"] == [
            {"name": "broken_no_stat"},
            {"stat_consumption": "sensor.fridge_energy"},
        ]

    async def test_add_source_succeeds_with_invalid_pre_existing(self, tools):
        invalid_prefs = {
            # An energy_sources entry missing the required stat_energy_from
            # for non-grid types (would fail local _shape_check today).
            "energy_sources": [{"type": "solar"}],
            "device_consumption": [],
            "device_consumption_water": [],
        }
        new_source = {
            "type": "battery",
            "stat_energy_from": "sensor.battery_in",
            "stat_energy_to": "sensor.battery_out",
        }

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": invalid_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_source", source=new_source
        )
        assert result["success"] is True
        save_payload = tools._client.send_websocket_message.call_args_list[
            1
        ].args[0]
        assert save_payload["energy_sources"][1] == new_source

    async def test_remove_device_succeeds_with_invalid_pre_existing(self, tools):
        invalid_prefs = {
            "energy_sources": [],
            "device_consumption": [
                {"stat_consumption": "sensor.fridge_energy"},
                {"name": "broken_no_stat"},  # invalid sibling
            ],
            "device_consumption_water": [],
        }

        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": invalid_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="remove_device", stat_consumption="sensor.fridge_energy"
        )
        assert result["success"] is True
        # The unaffected (still-broken) entry remains in the saved payload.
        save_payload = tools._client.send_websocket_message.call_args_list[
            1
        ].args[0]
        assert save_payload["device_consumption"] == [
            {"name": "broken_no_stat"},
        ]

    @pytest.mark.parametrize(
        "mode, kwargs, invalid_prefs",
        [
            (
                "add_device",
                {"stat_consumption": "sensor.new"},
                {
                    "energy_sources": [],
                    "device_consumption": [{"name": "broken_no_stat"}],
                    "device_consumption_water": [],
                },
            ),
            (
                "add_source",
                {
                    "source": {
                        "type": "battery",
                        "stat_energy_from": "sensor.battery_in",
                        "stat_energy_to": "sensor.battery_out",
                    },
                },
                {
                    "energy_sources": [{"type": "solar"}],  # missing stat_energy_from
                    "device_consumption": [],
                    "device_consumption_water": [],
                },
            ),
            (
                "remove_device",
                {"stat_consumption": "sensor.fridge_energy"},
                {
                    "energy_sources": [],
                    "device_consumption": [
                        {"stat_consumption": "sensor.fridge_energy"},
                        {"name": "broken_no_stat"},  # broken sibling, not removed
                    ],
                    "device_consumption_water": [],
                },
            ),
        ],
        ids=["add_device", "add_source", "remove_device"],
    )
    async def test_dry_run_succeeds_with_invalid_pre_existing(
        self, tools, mode, kwargs, invalid_prefs
    ):
        """Dry-run path's backstop ``_shape_check`` is scoped to the
        appended tail (mirroring the real-run path) for every convenience-
        mode mutator. Symmetry with the real-run regression block above
        guards against future divergence between the two ``_mutate_atomic``
        branches.
        """
        tools._client.send_websocket_message.return_value = {
            "success": True,
            "result": invalid_prefs,
        }

        result = await tools.ha_manage_energy_prefs(
            mode=mode, dry_run=True, **kwargs
        )
        assert result["success"] is True
        assert result["dry_run"] is True

    async def test_direct_set_mode_still_validates_full_config_when_validate_only_is_none(
        self, tools
    ):
        """Boundary pin: the ``validate_only`` scoping only applies on
        convenience-mode write paths (``add_*``/``remove_*``), which thread
        an explicit ``validate_only={target_key: appended_indices}`` through
        ``_set_prefs``. Direct ``mode='set'`` keeps the original full-
        validation contract (``validate_only=None`` default), so a
        snapshot's broken sibling still surfaces — the fix does NOT
        silently relax the direct-set path.

        The convenience helpers' "bad new entry" surface is closed by
        structural-by-construction (``_add_device`` builds a well-formed
        entry from typed parameters); see ``TestSetPrefs.test_shape_error_*``
        for the direct-set per-entry coverage.
        """
        invalid_prefs = {
            "energy_sources": [],
            "device_consumption": [{"name": "broken_no_stat"}],
            "device_consumption_water": [],
        }
        full_hash = compute_config_hash(invalid_prefs)
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": invalid_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="set",
                config=invalid_prefs,
                config_hash=full_hash,
            )
        err = json.loads(str(exc_info.value))
        # mode='set' direct path is unchanged: full _shape_check runs and
        # surfaces the broken sibling.
        assert "VALIDATION_FAILED" in json.dumps(err)


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
        assert err["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
        assert "stat_consumption" in err["error"]["message"].lower()

    async def test_happy_path_appends_to_device_consumption(self, tools):
        current_prefs = _sample_prefs()  # has fridge_energy
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},  # 1. initial _get_prefs
            {"success": True, "result": None},  # 2. save_prefs
            {"success": True, "result": _empty_validate_result()},  # 3. post-save
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.tv_energy",
            name="TV",
            included_in_stat="sensor.whole_home_energy",
        )

        assert result["success"] is True
        assert result["mode"] == "add_device"
        assert result["target_key"] == "device_consumption"
        assert result["new_count"] == 2  # fridge + tv

        # Convenience path skips _set_prefs's internal re-read — exactly
        # 3 WS calls per write attempt (initial get + save + validate).
        assert tools._client.send_websocket_message.call_count == 3

        # The save payload must be a partial — only device_consumption.
        save_call = tools._client.send_websocket_message.call_args_list[1]
        save_payload = save_call.args[0]
        assert save_payload["type"] == "energy/save_prefs"
        assert "device_consumption" in save_payload
        # Per-key full-replace semantics — other keys must NOT be in the save payload.
        assert "energy_sources" not in save_payload
        assert "device_consumption_water" not in save_payload
        # New entry shape — name and included_in_stat both land in the payload.
        new_devices = save_payload["device_consumption"]
        assert len(new_devices) == 2
        assert new_devices[1] == {
            "stat_consumption": "sensor.tv_energy",
            "name": "TV",
            "included_in_stat": "sensor.whole_home_energy",
        }

    async def test_water_flag_targets_water_list(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
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
        save_payload = tools._client.send_websocket_message.call_args_list[1].args[0]
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
        assert err["error"]["code"] == "RESOURCE_ALREADY_EXISTS"
        assert "sensor.fridge_energy" in err["error"]["message"]

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
        runs before the dry-run short-circuit)."""
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
        assert err["error"]["code"] == "RESOURCE_ALREADY_EXISTS"
        assert "sensor.fridge_energy" in err["error"]["message"]
        # Only the initial read; no save_prefs.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_dry_run_does_not_enter_retry_loop(self, tools):
        """dry_run short-circuits before the retry loop — exactly one WS
        call (the initial read) regardless of max_attempts."""
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]
        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.tv_energy",
            dry_run=True,
        )
        assert result["dry_run"] is True
        # If dry_run had entered the retry loop and re-read, this would be 2.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_fresh_install_no_prefs_starts_empty(self, tools):
        # First call returns "No prefs"; tool maps to default empty.
        # _set_prefs no longer re-reads on the convenience path, so only
        # 3 WS calls total (initial get + save + validate).
        tools._client.send_websocket_message.side_effect = [
            {"success": False, "error": "Command failed: No prefs"},  # initial get
            {"success": True, "result": None},  # save
            {"success": True, "result": _empty_validate_result()},  # post-save
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.fridge_energy",
        )

        assert result["success"] is True
        assert result["new_count"] == 1
        assert tools._client.send_websocket_message.call_count == 3


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="remove_device"
# -----------------------------------------------------------------------------


class TestRemoveDevice:
    async def test_missing_stat_consumption_raises(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(mode="remove_device")
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "VALIDATION_MISSING_PARAMETER"

    async def test_happy_path_removes_entry(self, tools):
        current_prefs = _sample_prefs()  # has sensor.fridge_energy
        tools._client.send_websocket_message.side_effect = [
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
        save_payload = tools._client.send_websocket_message.call_args_list[1].args[0]
        assert save_payload["device_consumption"] == []
        assert tools._client.send_websocket_message.call_count == 3

    async def test_water_flag_targets_water_list_for_remove(self, tools):
        """remove_device with water=True targets device_consumption_water,
        mirroring the add_device water-flag semantics."""
        current_prefs = {
            **_sample_prefs(),
            "device_consumption_water": [
                {"stat_consumption": "sensor.water_meter"},
            ],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="remove_device",
            stat_consumption="sensor.water_meter",
            water=True,
        )

        assert result["target_key"] == "device_consumption_water"
        assert result["new_count"] == 0
        save_payload = tools._client.send_websocket_message.call_args_list[1].args[0]
        # Only the water list is touched; the electricity list is preserved
        # by virtue of being absent from the partial save payload.
        assert "device_consumption_water" in save_payload
        assert save_payload["device_consumption_water"] == []
        assert "device_consumption" not in save_payload

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
        assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "sensor.does_not_exist" in err["error"]["message"]

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
        assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "sensor.does_not_exist" in err["error"]["message"]
        assert tools._client.send_websocket_message.call_count == 1


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — mode="add_source"
# -----------------------------------------------------------------------------


class TestAddSource:
    async def test_missing_source_raises(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(mode="add_source")
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
        assert "source" in err["error"]["message"].lower()

    async def test_invalid_type_raises_validation_failed(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_source",
                source={"type": "wind"},  # not in valid_types
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.parametrize("source_type", ["solar", "battery", "gas"])
    async def test_non_grid_missing_stat_energy_from_raises(self, tools, source_type):
        """solar/battery/gas all require stat_energy_from; grid does not."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_source",
                source={"type": source_type},
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "VALIDATION_FAILED"
        assert "stat_energy_from" in json.dumps(err["error"])

    async def test_grid_happy_path(self, tools):
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
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
        save_payload = tools._client.send_websocket_message.call_args_list[1].args[0]
        assert "energy_sources" in save_payload
        assert "device_consumption" not in save_payload
        assert save_payload["energy_sources"][-1] == new_grid

    @pytest.mark.parametrize("source_type", ["solar", "battery", "gas"])
    async def test_non_grid_happy_path(self, tools, source_type):
        """solar/battery/gas append to energy_sources just like grid does."""
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        new_source = {
            "type": source_type,
            "stat_energy_from": f"sensor.{source_type}_in",
        }
        result = await tools.ha_manage_energy_prefs(
            mode="add_source",
            source=new_source,
        )

        assert result["success"] is True
        assert result["target_key"] == "energy_sources"
        save_payload = tools._client.send_websocket_message.call_args_list[1].args[0]
        assert save_payload["energy_sources"][-1] == new_source

    @pytest.mark.parametrize("source_type", ["solar", "battery", "gas"])
    async def test_non_grid_duplicate_raises_already_exists(self, tools, source_type):
        """solar/battery/gas reject duplicates by (type, stat_energy_from)."""
        stat = f"sensor.{source_type}_existing"
        current_prefs = {
            **_sample_prefs(),
            "energy_sources": [
                # Pre-existing entry with the same (type, stat_energy_from).
                {"type": source_type, "stat_energy_from": stat},
            ],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_source",
                source={"type": source_type, "stat_energy_from": stat},
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "RESOURCE_ALREADY_EXISTS"
        # create_error_response spreads context fields onto the top-level
        # response, not under err["error"]["context"].
        assert err["type"] == source_type
        assert err["stat_energy_from"] == stat
        # No save call — duplicate detection short-circuits before _set_prefs.
        assert tools._client.send_websocket_message.call_count == 1

    async def test_grid_does_not_check_duplicates(self, tools):
        """Grid sources can have multiple legitimate variants (e.g. different
        tariffs); the helper appends without a duplicate check."""
        existing_grid = {"type": "grid", "stat_energy_from": "sensor.grid_main"}
        current_prefs = {
            **_sample_prefs(),
            "energy_sources": [existing_grid],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},
            {"success": True, "result": _empty_validate_result()},
        ]

        # Identical grid entry: must NOT raise — caller is responsible for
        # de-duplication on grid.
        result = await tools.ha_manage_energy_prefs(
            mode="add_source",
            source=dict(existing_grid),  # same payload
        )

        assert result["success"] is True
        save_payload = tools._client.send_websocket_message.call_args_list[1].args[0]
        # Two grid entries now (caller's choice).
        assert len(save_payload["energy_sources"]) == 2

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

    async def test_dry_run_raises_on_duplicate_non_grid(self, tools):
        """dry_run on a duplicate solar/battery/gas raises before the
        dry-run short-circuit, matching the add_device dry_run semantic."""
        current_prefs = {
            **_sample_prefs(),
            "energy_sources": [
                {"type": "solar", "stat_energy_from": "sensor.solar_panel"},
            ],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
        ]
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_source",
                source={"type": "solar", "stat_energy_from": "sensor.solar_panel"},
                dry_run=True,
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "RESOURCE_ALREADY_EXISTS"
        assert tools._client.send_websocket_message.call_count == 1


# -----------------------------------------------------------------------------
# ha_manage_energy_prefs — convenience-mode hash-conflict retry
# -----------------------------------------------------------------------------


def _resource_locked_error() -> ToolError:
    """Build a ToolError with the structured RESOURCE_LOCKED payload that
    ``_set_prefs`` raises on a hash mismatch.

    Mirrors the JSON shape produced by ``raise_tool_error`` so the retry
    loop's parser sees the same code path as in production.
    """
    return ToolError(
        json.dumps(
            {
                "success": False,
                "error": {
                    "code": "RESOURCE_LOCKED",
                    "message": "Energy prefs modified since last read (conflict)",
                    "context": {"mode": "set"},
                },
            }
        )
    )


class TestConvenienceRetryOnHashConflict:
    """The convenience path threads the freshly-fetched snapshot into
    ``_set_prefs`` so the inner re-read + hash check is skipped — which
    means a real WS sequence cannot organically produce RESOURCE_LOCKED on
    the convenience path. To exercise the retry mechanism in isolation we
    monkeypatch ``_set_prefs`` directly. This decouples the retry semantics
    from the snapshot-threading optimisation."""

    async def test_add_device_retries_once_on_hash_conflict(self, tools, monkeypatch):
        """First _set_prefs call raises RESOURCE_LOCKED; retry loop reads
        fresh and the second _set_prefs call succeeds."""
        prefs = _sample_prefs()
        # Two _get_prefs WS calls (one per attempt); no save calls hit the
        # WS layer because _set_prefs itself is mocked.
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": prefs},
            {"success": True, "result": prefs},
        ]

        success_response = {
            "success": True,
            "mode": "set",
            "config_hash": "newhash" + "0" * 9,
            "message": "Energy prefs updated.",
        }
        set_prefs_mock = AsyncMock(
            side_effect=[_resource_locked_error(), success_response]
        )
        monkeypatch.setattr(tools, "_set_prefs", set_prefs_mock)

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.tv_energy",
        )
        assert result["success"] is True
        assert result["config_hash"] == "newhash" + "0" * 9
        # Two _get_prefs reads, two _set_prefs invocations.
        assert tools._client.send_websocket_message.call_count == 2
        assert set_prefs_mock.call_count == 2

    async def test_retry_exhaustion_raises_resource_locked(self, tools, monkeypatch):
        """Both attempts' _set_prefs raise RESOURCE_LOCKED. On the final
        iteration the retry gate (``attempt + 1 < max_attempts``) is False,
        so the bare ``raise`` propagates the error to the caller."""
        prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": prefs},
            {"success": True, "result": prefs},
        ]
        set_prefs_mock = AsyncMock(
            side_effect=[_resource_locked_error(), _resource_locked_error()]
        )
        monkeypatch.setattr(tools, "_set_prefs", set_prefs_mock)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_device",
                stat_consumption="sensor.tv_energy",
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "RESOURCE_LOCKED"
        assert tools._client.send_websocket_message.call_count == 2
        assert set_prefs_mock.call_count == 2

    async def test_cross_attempt_mutator_divergence_raises_already_exists(
        self, tools, monkeypatch
    ):
        """Attempt 1 hits RESOURCE_LOCKED; on the retry, the fresh read
        already contains the device (concurrent writer added it), so the
        mutator raises RESOURCE_ALREADY_EXISTS — that propagates to the
        caller instead of RESOURCE_LOCKED. Pins the semantic that the
        retry's mutator runs against fresh state, not the original."""
        prefs_v1 = _sample_prefs()  # has fridge_energy
        prefs_v2 = {  # concurrent writer adds tv_energy between our attempts
            **prefs_v1,
            "device_consumption": [
                *prefs_v1["device_consumption"],
                {"stat_consumption": "sensor.tv_energy"},
            ],
        }
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": prefs_v1},  # attempt 1 read
            {"success": True, "result": prefs_v2},  # attempt 2 read (post-conflict)
        ]

        # Attempt 1's _set_prefs raises RESOURCE_LOCKED → retry. Attempt 2
        # never reaches _set_prefs because the mutator on the fresh read
        # detects the duplicate and raises RESOURCE_ALREADY_EXISTS.
        set_prefs_mock = AsyncMock(side_effect=[_resource_locked_error()])
        monkeypatch.setattr(tools, "_set_prefs", set_prefs_mock)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_device",
                stat_consumption="sensor.tv_energy",
            )
        err = json.loads(str(exc_info.value))
        assert err["error"]["code"] == "RESOURCE_ALREADY_EXISTS"
        assert "sensor.tv_energy" in err["error"]["message"]
        # Two reads, exactly one _set_prefs invocation (attempt 1).
        assert tools._client.send_websocket_message.call_count == 2
        assert set_prefs_mock.call_count == 1

    async def test_retry_log_level_is_warning(self, tools, monkeypatch, caplog):
        """The retry log line uses logger.warning (concurrent-modification
        events are operational signal, not info-level chatter)."""
        import logging

        prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": prefs},
            {"success": True, "result": prefs},
        ]
        success_response = {
            "success": True,
            "mode": "set",
            "config_hash": "newhash" + "0" * 9,
            "message": "Energy prefs updated.",
        }
        set_prefs_mock = AsyncMock(
            side_effect=[_resource_locked_error(), success_response]
        )
        monkeypatch.setattr(tools, "_set_prefs", set_prefs_mock)

        with caplog.at_level(logging.WARNING, logger="ha_mcp.tools.tools_energy"):
            await tools.ha_manage_energy_prefs(
                mode="add_device",
                stat_consumption="sensor.tv_energy",
            )
        retry_records = [
            rec for rec in caplog.records if "hash conflict on attempt" in rec.message
        ]
        assert len(retry_records) == 1
        assert retry_records[0].levelno == logging.WARNING


# -----------------------------------------------------------------------------
# Convenience-mode Pattern-A wrapping (non-ToolError exceptions)
# -----------------------------------------------------------------------------


class TestConveniencePatternA:
    """Pattern-A wraps non-ToolError exceptions raised inside
    ``_mutate_atomic``'s own scope into structured errors carrying
    ``context={'mode': ..., 'target_key': ...}``.

    Note: ``_get_prefs`` and ``_set_prefs`` each have their own Pattern-A
    wrappers, so a non-ToolError raised inside either of them is caught
    there and converted to a ToolError with their context — it never
    reaches ``_mutate_atomic``'s outer ``except Exception``. To exercise
    ``_mutate_atomic``'s wrapper specifically, we monkeypatch
    ``_set_prefs`` to raise a raw exception that propagates up.
    """

    async def test_non_tool_error_from_set_prefs_wrapped_with_context(
        self, tools, monkeypatch
    ):
        prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": prefs},
        ]
        # A raw ConnectionError from _set_prefs (not a ToolError) bypasses
        # the inner ``except ToolError`` retry gate and is caught by the
        # outer Pattern-A in _mutate_atomic.
        monkeypatch.setattr(
            tools,
            "_set_prefs",
            AsyncMock(side_effect=ConnectionError("ws reset mid-save")),
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_energy_prefs(
                mode="add_device",
                stat_consumption="sensor.tv_energy",
            )
        err = json.loads(str(exc_info.value))
        assert err["success"] is False
        # create_error_response spreads context fields onto the top-level
        # response (see errors.py:256-258); they're NOT nested under
        # err["error"]["context"].
        assert err["mode"] == "add_device"
        assert err["target_key"] == "device_consumption"

    async def test_non_tool_error_from_mutator_wrapped_with_context(self, tools):
        """A non-ToolError raised inside the mutator itself (run inline in
        ``_mutate_atomic``'s scope) hits the same Pattern-A wrapper."""
        prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": prefs},
        ]

        def bad_mutator(_existing):
            raise RuntimeError("mutator blew up")

        with pytest.raises(ToolError) as exc_info:
            await tools._mutate_atomic(
                mode="add_device",
                target_key="device_consumption",
                mutator=bad_mutator,
                dry_run=False,
                preview_payload={},
            )
        err = json.loads(str(exc_info.value))
        assert err["mode"] == "add_device"
        assert err["target_key"] == "device_consumption"


# -----------------------------------------------------------------------------
# Convenience-mode response passthrough
# -----------------------------------------------------------------------------


class TestConvenienceResponsePassthrough:
    """``_mutate_atomic`` filters its response by overriding a closed set of
    keys (success/mode/config_hash/target_key/new_count/message) and passing
    everything else from ``_set_prefs`` through. This guards against the
    convenience-mode response silently diverging from ``mode='set'`` when
    new optional fields are added to ``_set_prefs``."""

    async def test_post_save_validation_errors_bubble_through(self, tools):
        current_prefs = _sample_prefs()
        # Post-save validate returns errors → _set_prefs sets
        # post_save_validation_errors + warning. Both must reach the
        # convenience-mode response.
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},  # save
            {
                "success": True,
                "result": {
                    "energy_sources": [],
                    "device_consumption": [[], ["stat not found"]],
                    "device_consumption_water": [],
                },
            },
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.tv_energy",
        )
        assert result["success"] is True
        assert "post_save_validation_errors" in result
        assert len(result["post_save_validation_errors"]) == 1
        assert "warning" in result

    async def test_partial_warning_bubbles_through(self, tools):
        """When post-save validate itself fails, _set_prefs sets
        partial=True + a warning. Both must surface on the convenience
        response."""
        current_prefs = _sample_prefs()
        tools._client.send_websocket_message.side_effect = [
            {"success": True, "result": current_prefs},
            {"success": True, "result": None},  # save
            {"success": False, "error": "validate broken"},  # post-save validate
        ]

        result = await tools.ha_manage_energy_prefs(
            mode="add_device",
            stat_consumption="sensor.tv_energy",
        )
        assert result["success"] is True
        assert result["partial"] is True
        assert "validate broken" in result["warning"]
