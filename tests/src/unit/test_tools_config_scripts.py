"""
Unit tests for Script configuration tools.

These tests verify the input validation and error handling of the script tools,
especially for blueprint-based scripts (issue #466).
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import entity_registration
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools


@pytest.fixture(autouse=True)
def no_registration_retry(monkeypatch):
    """Use one lookup by default; polling tests opt in with registration_clock."""
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)


@pytest.fixture
def registration_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)

    async def sleep(delay):
        clock.now += delay

    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 1.0)
    monkeypatch.setattr(
        entity_registration, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    monkeypatch.setattr(
        entity_registration,
        "asyncio",
        SimpleNamespace(sleep=sleep, timeout=asyncio.timeout),
    )
    return clock


class TestScriptPostWriteWait:
    @pytest.fixture
    def tools(self):
        client = MagicMock()
        client.upsert_script_config = AsyncMock(
            return_value={"success": True, "script_id": "storage_key"}
        )
        client.get_services = AsyncMock(return_value=[])
        client.get_states = AsyncMock(return_value=[])
        client.get_entity_state = AsyncMock(return_value={"state": "off"})
        tools = ConfigScriptTools(client)
        tools._fetch_and_verify_hash = AsyncMock(
            return_value=({"sequence": [{"delay": 1}]}, "storage_key")
        )
        tools._get_script_config_internal = AsyncMock(
            return_value=({}, "newhash", "storage_key")
        )
        return tools

    @pytest.fixture(params=[False, True], ids=["full_config", "transform"])
    def write_arguments(self, request):
        if request.param:
            return {
                "python_transform": "config['mode'] = 'single'",
                "config_hash": "prior_hash",
            }
        return {"config": {"sequence": [{"delay": 1}]}}

    async def test_wait_without_category_uses_delayed_registered_entity(
        self, tools, write_arguments, monkeypatch, registration_clock
    ):
        lookup = AsyncMock(
            side_effect=[
                [],
                [],
                [],
                [
                    {
                        "entity_id": "script.current_name",
                        "unique_id": "storage_key",
                        "platform": "script",
                    }
                ],
            ]
        )
        monkeypatch.setattr(
            entity_registration, "fetch_entity_lookup_via_component", lookup
        )
        registered = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_registered", registered
        )

        result = await tools.ha_config_set_script(
            script_id="caller_alias", **write_arguments
        )

        assert result["success"] is True
        registered.assert_awaited_once_with(tools._client, "script.current_name")
        lookup.assert_awaited_with(tools._client, "storage_key", domain="script")
        assert lookup.await_count == 4

    async def test_wait_false_without_category_skips_lookup_and_wait(
        self, tools, write_arguments, monkeypatch
    ):
        lookup = AsyncMock()
        registered = AsyncMock()
        monkeypatch.setattr(
            entity_registration, "fetch_entity_lookup_via_component", lookup
        )
        monkeypatch.setattr(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_registered", registered
        )

        result = await tools.ha_config_set_script(
            script_id="caller_alias", wait=False, **write_arguments
        )

        assert result["success"] is True
        lookup.assert_not_awaited()
        registered.assert_not_awaited()

    @pytest.mark.parametrize("connection_failure", [False, True])
    async def test_wait_failure_keeps_write_success_and_warning(
        self, tools, write_arguments, monkeypatch, connection_failure
    ):
        monkeypatch.setattr(
            entity_registration,
            "fetch_entity_lookup_via_component",
            AsyncMock(return_value=[]),
        )
        registered = AsyncMock(
            return_value=False,
            side_effect=HomeAssistantConnectionError("offline")
            if connection_failure
            else None,
        )
        monkeypatch.setattr(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_registered", registered
        )

        result = await tools.ha_config_set_script(
            script_id="caller_alias", **write_arguments
        )

        assert result["success"] is True
        warning = "verification failed" if connection_failure else "not yet queryable"
        assert any(warning in item for item in result["warnings"])

    @pytest.mark.parametrize("api_failure", [False, True])
    @pytest.mark.parametrize("caller", ["caller_alias", "script.caller_alias"])
    async def test_lookup_failure_preserves_caller_alias_for_wait(
        self,
        tools,
        write_arguments,
        monkeypatch,
        registration_clock,
        api_failure,
        caller,
    ):
        lookup = AsyncMock(
            return_value=[],
            side_effect=HomeAssistantAPIError("offline") if api_failure else None,
        )
        monkeypatch.setattr(
            entity_registration, "fetch_entity_lookup_via_component", lookup
        )
        registered = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_registered", registered
        )

        result = await tools.ha_config_set_script(script_id=caller, **write_arguments)

        assert result["success"] is True
        registered.assert_awaited_once_with(tools._client, "script.caller_alias")
        if api_failure:
            assert lookup.await_count == 1
        else:
            assert lookup.await_count > 1


class TestScriptToolsValidation:
    """Test input validation for script configuration tools."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        client.upsert_script_config = AsyncMock(
            return_value={"success": True, "script_id": "test_script"}
        )
        client.get_script_config = AsyncMock(
            return_value={
                "alias": "Test Script",
                "sequence": [{"delay": {"seconds": 1}}],
            }
        )
        client.delete_script_config = AsyncMock(
            return_value={"success": True, "script_id": "test_script"}
        )
        client.get_entity_state = AsyncMock(
            return_value={"state": "off", "entity_id": "script.test_script"}
        )
        # Reference validator (#940) calls these during set_script;
        # provide empty-but-valid payloads so the walker runs.
        client.get_services = AsyncMock(return_value=[])
        client.get_states = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def tools(self, mock_client):
        """Create ConfigScriptTools instance."""
        return ConfigScriptTools(mock_client)

    async def test_set_script_missing_both_sequence_and_blueprint(self, tools):
        """Test that config without sequence or use_blueprint is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config={
                    "alias": "Test Script"
                },  # Missing both sequence and use_blueprint
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error_msg = error_data["error"]["message"]
        assert "sequence" in error_msg and "use_blueprint" in error_msg

    async def test_set_script_with_sequence_success(self, tools, mock_client):
        """Test that regular script with sequence is accepted."""
        result = await tools.ha_config_set_script(
            script_id="test_script",
            config={
                "alias": "Test Script",
                "sequence": [{"delay": {"seconds": 5}}],
            },
        )

        assert result["success"] is True
        mock_client.upsert_script_config.assert_called_once()

    async def test_set_script_with_blueprint_success(self, tools, mock_client):
        """Test that blueprint-based script is accepted."""
        result = await tools.ha_config_set_script(
            script_id="test_script",
            config={
                "alias": "My Blueprint Script",
                "use_blueprint": {
                    "path": "notification_script.yaml",
                    "input": {"message": "Hello"},
                },
            },
        )

        assert result["success"] is True
        mock_client.upsert_script_config.assert_called_once()

        # Verify the config passed to client doesn't have empty sequence
        call_args = mock_client.upsert_script_config.call_args
        config_passed = call_args[0][0]
        assert "use_blueprint" in config_passed
        assert "sequence" not in config_passed or config_passed["sequence"] != []

    async def test_set_script_blueprint_with_empty_sequence_strips_it(
        self, tools, mock_client
    ):
        """Test that empty sequence is stripped from blueprint scripts."""
        result = await tools.ha_config_set_script(
            script_id="test_script",
            config={
                "alias": "My Blueprint Script",
                "use_blueprint": {
                    "path": "notification_script.yaml",
                    "input": {"message": "Hello"},
                },
                "sequence": [],  # Empty sequence should be stripped
            },
        )

        assert result["success"] is True

        # Verify empty sequence was stripped
        call_args = mock_client.upsert_script_config.call_args
        config_passed = call_args[0][0]
        assert "sequence" not in config_passed, "Empty sequence should be stripped"

    async def test_set_script_blueprint_with_non_empty_sequence_keeps_it(
        self, tools, mock_client
    ):
        """Test that non-empty sequence is kept even with blueprint."""
        result = await tools.ha_config_set_script(
            script_id="test_script",
            config={
                "alias": "My Blueprint Script",
                "use_blueprint": {
                    "path": "notification_script.yaml",
                    "input": {"message": "Hello"},
                },
                "sequence": [{"delay": {"seconds": 1}}],  # Non-empty should be kept
            },
        )

        assert result["success"] is True

        # Verify non-empty sequence was kept
        call_args = mock_client.upsert_script_config.call_args
        config_passed = call_args[0][0]
        assert "sequence" in config_passed
        assert config_passed["sequence"] == [{"delay": {"seconds": 1}}]

    async def test_set_script_invalid_json_config(self, tools):
        """Test that invalid JSON config is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config='{"invalid": json}',  # Invalid JSON string
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Invalid config parameter" in error_data["error"]["message"]

    async def test_set_script_config_not_dict(self, tools):
        """Test that non-dict config is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config="not a dict",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        # The error message comes from parse_json_param which tries to parse as JSON first
        assert "Invalid" in error_data["error"]["message"]


class TestGetScriptCanonicalId:
    """Issue #1334: returned ``script_id`` is the canonical storage key,
    falling back to the caller input when the rest_client envelope omits it."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        # send_websocket_message is invoked by fetch_entity_category; return
        # a no-category response so the get path doesn't error.
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        return client

    @pytest.fixture
    def tools(self, mock_client):
        return ConfigScriptTools(mock_client)

    async def test_returns_canonical_script_id_from_envelope(self, tools, mock_client):
        """When the rest_client resolves an alias to a storage key, the
        tool's returned ``script_id`` reflects the canonical key."""
        mock_client.get_script_config = AsyncMock(
            return_value={
                "success": True,
                "script_id": "1234567890",
                "config": {
                    "alias": "Morning Routine",
                    "sequence": [{"delay": {"seconds": 1}}],
                },
            }
        )

        result = await tools.ha_config_get_script(script_id="morning_routine")

        assert result["success"] is True
        assert result["action"] == "get"
        assert result["script_id"] == "1234567890"
        assert "config_hash" in result

    async def test_falls_back_to_input_and_warns_when_envelope_missing_key(
        self, tools, mock_client, caplog
    ):
        """If the rest_client envelope omits ``script_id`` (contract
        violation), the tool surfaces the caller-supplied identifier and
        logs a warning rather than masking the missing key silently."""
        mock_client.get_script_config = AsyncMock(
            return_value={
                "success": True,
                "config": {
                    "alias": "Test",
                    "sequence": [{"delay": {"seconds": 1}}],
                },
            }
        )

        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.tools_config_scripts"
        ):
            result = await tools.ha_config_get_script(script_id="caller_input")

        assert result["success"] is True
        assert result["action"] == "get"
        assert result["script_id"] == "caller_input"
        assert any(
            "rest_client contract violation" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        ), (
            f"Expected contract-violation warning, got: {[r.message for r in caplog.records]}"
        )


class TestGetScriptReturnsTheBody:
    """``config`` is the script body, not the REST envelope (#2329).

    It used to be the envelope, so ``result["config"]`` was
    ``{success, script_id, config}`` and ``result["config"]["sequence"]`` did
    not exist -- callers had to unwrap twice, and the e2e carried a helper
    that did exactly that. ``ha_config_get_automation`` returns the body and
    this tool's docstring promises the body.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        client.get_script_config = AsyncMock(
            return_value={
                "success": True,
                "script_id": "1234567890",
                "config": {
                    "alias": "Morning Routine",
                    "sequence": [{"delay": {"seconds": 1}}],
                },
            }
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        return ConfigScriptTools(mock_client)

    async def test_config_is_the_script_body(self, tools):
        result = await tools.ha_config_get_script(script_id="morning_routine")

        config = result["config"]
        assert config["alias"] == "Morning Routine"
        assert config["sequence"] == [{"delay": {"seconds": 1}}]

    async def test_the_envelope_does_not_leak_into_config(self, tools):
        """A second ``config`` key under ``config`` is the old double-wrap."""
        result = await tools.ha_config_get_script(script_id="morning_routine")

        assert "config" not in result["config"]
        assert "success" not in result["config"]
        assert "script_id" not in result["config"]

    async def test_category_is_injected_into_the_body(self, tools, mock_client):
        """The category has to ride inside ``config`` now that it is the body."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"categories": {"script": "cat-123"}},
            }
        )

        result = await tools.ha_config_get_script(script_id="morning_routine")

        assert result["config"].get("category") == "cat-123"


class TestScriptUpsertResolvedThreading:
    """Issue #1813 Phase 0 item #6: when the tool pre-resolves the storage key
    (its hash-verify fetch already resolved it via the REST envelope), it passes
    that key as ``resolved_id`` to ``upsert_script_config`` so the REST client
    skips the redundant second registry lookup — while ``script_id`` stays the
    CALLER's identifier so a renamed script's alias is not reset to the storage
    key (#1935). The no-hash path passes ``resolved_id=None`` (resolved once,
    inside the upsert).
    """

    # The inner script body the stubbed envelope carries; its hash is the
    # optimistic-locking token the tool verifies before writing.
    INNER_CONFIG: ClassVar[dict[str, Any]] = {
        "alias": "Morning",
        "sequence": [{"delay": {"seconds": 1}}],
    }

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        client.get_entity_state = AsyncMock(return_value={"state": "off"})
        # get_script_config resolves the alias "renamed_script" to storage key
        # "storage_key" (mirrors a UI-renamed script), returning the REST
        # envelope shape ha_config_set_script's fetch consumes.
        client.get_script_config = AsyncMock(
            return_value={
                "success": True,
                "script_id": "storage_key",
                "config": dict(self.INNER_CONFIG),
            }
        )
        client.upsert_script_config = AsyncMock(
            return_value={"success": True, "script_id": "storage_key"}
        )
        # Reference validator (#940) walks these during set_script.
        client.get_services = AsyncMock(return_value=[])
        client.get_states = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def tools(self, mock_client):
        return ConfigScriptTools(mock_client)

    @staticmethod
    def _seed_hash():
        from ha_mcp.utils.config_hash import compute_config_hash

        return compute_config_hash(dict(TestScriptUpsertResolvedThreading.INNER_CONFIG))

    async def test_full_config_with_hash_threads_resolved_id(self, tools, mock_client):
        """A full-config update supplying ``config_hash`` pre-resolves via the
        hash-verify fetch → the resolved storage key is passed as ``resolved_id``
        (write target) while ``script_id`` stays the caller's id."""
        result = await tools.ha_config_set_script(
            script_id="renamed_script",
            config={"alias": "Morning", "sequence": [{"delay": {"seconds": 5}}]},
            config_hash=self._seed_hash(),
            wait=False,
        )

        assert result["success"] is True
        args, kwargs = mock_client.upsert_script_config.call_args
        assert kwargs.get("resolved_id") == "storage_key"
        assert args[1] == "renamed_script"

    async def test_python_transform_threads_resolved_id(self, tools, mock_client):
        """python_transform always hash-verifies first → threaded the same way."""
        result = await tools.ha_config_set_script(
            script_id="renamed_script",
            python_transform="config['mode'] = 'single'",
            config_hash=self._seed_hash(),
        )

        assert result["success"] is True
        args, kwargs = mock_client.upsert_script_config.call_args
        assert kwargs.get("resolved_id") == "storage_key"
        assert args[1] == "renamed_script"

    async def test_renamed_config_hash_no_alias_forwards_caller_id_for_alias(
        self, tools, mock_client
    ):
        """#1935: a renamed script updated with config_hash + a config omitting
        ``alias`` forwards the CALLER's id as ``script_id`` (the REST client's
        alias-default source) and the resolved storage key as ``resolved_id``
        (write target) — so the alias is not reset to the storage key, and the
        resolver runs only once (in the hash-verify fetch)."""
        result = await tools.ha_config_set_script(
            script_id="renamed_script",
            config={"sequence": [{"delay": {"seconds": 5}}]},  # no alias
            config_hash=self._seed_hash(),
            wait=False,
        )

        assert result["success"] is True
        args, kwargs = mock_client.upsert_script_config.call_args
        # Caller id is the alias-default source; storage key is the write target.
        assert args[1] == "renamed_script"
        assert kwargs.get("resolved_id") == "storage_key"
        # The config forwarded to the REST client still has no alias — the
        # default is applied inside upsert_script_config from ``script_id``
        # (proven by TestUpsertScriptResolvedShortCircuit at the REST level).
        assert "alias" not in args[0]

    async def test_full_config_without_hash_is_not_threaded(self, tools, mock_client):
        """No ``config_hash`` → no pre-resolve → ``resolved_id=None``, the REST
        client resolves once from the caller ``script_id``."""
        result = await tools.ha_config_set_script(
            script_id="renamed_script",
            config={"alias": "Morning", "sequence": [{"delay": {"seconds": 5}}]},
            wait=False,
        )

        assert result["success"] is True
        args, kwargs = mock_client.upsert_script_config.call_args
        assert kwargs.get("resolved_id") is None
        assert args[1] == "renamed_script"

    async def test_missing_envelope_key_reresolves_not_caller_slug(
        self, tools, mock_client
    ):
        """Structural invariant: if the REST envelope omits ``script_id`` (never
        happens today — ``get_script_config`` always sets it), the tool passes
        ``resolved_id=None`` so the upsert RE-RESOLVES rather than threading the
        caller's unresolved slug as the write target (which for a renamed script
        would hit the wrong storage key)."""
        mock_client.get_script_config = AsyncMock(
            return_value={"success": True, "config": dict(self.INNER_CONFIG)}  # no key
        )

        result = await tools.ha_config_set_script(
            script_id="renamed_script",
            config={"alias": "Morning", "sequence": [{"delay": {"seconds": 5}}]},
            config_hash=self._seed_hash(),
            wait=False,
        )

        assert result["success"] is True
        args, kwargs = mock_client.upsert_script_config.call_args
        assert kwargs.get("resolved_id") is None  # re-resolve, not the caller slug
        assert args[1] == "renamed_script"


class TestStripEmptyScriptFields:
    """Test the _strip_empty_script_fields helper function."""

    def test_strip_empty_sequence(self):
        """Test that empty sequence array is removed."""
        from ha_mcp.tools.tools_config_scripts import _strip_empty_script_fields

        config = {
            "alias": "Test",
            "use_blueprint": {"path": "test.yaml", "input": {}},
            "sequence": [],
        }

        result = _strip_empty_script_fields(config)

        assert "sequence" not in result
        assert "use_blueprint" in result
        assert "alias" in result

    def test_keep_non_empty_sequence(self):
        """Test that non-empty sequence is kept."""
        from ha_mcp.tools.tools_config_scripts import _strip_empty_script_fields

        config = {
            "alias": "Test",
            "use_blueprint": {"path": "test.yaml", "input": {}},
            "sequence": [{"delay": {"seconds": 1}}],
        }

        result = _strip_empty_script_fields(config)

        assert "sequence" in result
        assert result["sequence"] == [{"delay": {"seconds": 1}}]

    def test_no_sequence_field(self):
        """Test that config without sequence is unchanged."""
        from ha_mcp.tools.tools_config_scripts import _strip_empty_script_fields

        config = {
            "alias": "Test",
            "use_blueprint": {"path": "test.yaml", "input": {}},
        }

        result = _strip_empty_script_fields(config)

        assert "sequence" not in result
        assert result == config


class TestSetScriptCategoryValidation:
    """A category that does not exist must not reach the script.

    ``apply_entity_category`` runs after the upsert and HA accepts any category
    ID, so an unvalidated typo used to leave the script created with a dangling
    category reference (issue #2159). Validation happens at tool entry so
    nothing is written when the category is wrong.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.upsert_script_config = AsyncMock(
            return_value={"success": True, "script_id": "test_script"}
        )
        client.get_entity_state = AsyncMock(
            return_value={"state": "off", "entity_id": "script.test_script"}
        )
        # Reference validator (#940) walks these during set_script.
        client.get_services = AsyncMock(return_value=[])
        client.get_states = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def tools(self, mock_client):
        return ConfigScriptTools(mock_client)

    @staticmethod
    def _ws_handler(*category_ids):
        """Answer the category-registry preflight; ack everything else."""

        async def handler(msg):
            if msg.get("type") == "config/category_registry/list":
                return {
                    "success": True,
                    "result": [{"category_id": cid} for cid in category_ids],
                }
            if msg.get("type") == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "script.test_script",
                            "unique_id": "test_script",
                            "platform": "script",
                        }
                    ],
                }
            return {"success": True, "result": {"categories": {}}}

        return handler

    @pytest.fixture
    def sequence_config(self):
        return {"alias": "Test Script", "sequence": [{"delay": {"seconds": 1}}]}

    async def test_unknown_category_param_rejected_before_upsert(
        self, tools, mock_client, sequence_config
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_handler("lighting")
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config=sequence_config,
                category="ghost_category",
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error_data["category"] == "ghost_category"
        assert error_data["scope"] == "script"
        mock_client.upsert_script_config.assert_not_called()

    async def test_unknown_category_in_config_rejected_before_upsert(
        self, tools, mock_client, sequence_config
    ):
        """The config dict is the second category source — it needs the same gate."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_handler("lighting")
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config={**sequence_config, "category": "ghost_category"},
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        mock_client.upsert_script_config.assert_not_called()

    async def test_category_registry_lookup_failure_fails_closed(
        self, tools, mock_client, sequence_config
    ):
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": {"message": "unavailable"}}
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config=sequence_config,
                category="lighting",
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "CONNECTION_FAILED"
        mock_client.upsert_script_config.assert_not_called()

    async def test_existing_category_proceeds(
        self, tools, mock_client, sequence_config
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_handler("lighting")
        )

        result = await tools.ha_config_set_script(
            script_id="test_script",
            config=sequence_config,
            category="lighting",
            wait=False,
        )

        assert result["success"] is True
        mock_client.upsert_script_config.assert_called_once()

    @pytest.fixture
    def transform_tools(self, tools, sequence_config):
        """Bypass hash arithmetic for python_transform tests (mirrors the
        automations transform fixture)."""
        tools._fetch_and_verify_hash = AsyncMock(
            return_value=(dict(sequence_config), "test_script")
        )
        tools._get_script_config_internal = AsyncMock(
            return_value=({}, "newhash", None)
        )
        return tools

    async def test_unknown_category_rejected_on_python_transform(
        self, transform_tools, mock_client
    ):
        """The transform path applies a category too — and must gate it too."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_handler("lighting")
        )

        with pytest.raises(ToolError) as exc_info:
            await transform_tools.ha_config_set_script(
                script_id="test_script",
                python_transform="config['mode'] = 'single'",
                config_hash="prior_hash",
                category="ghost_category",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        mock_client.upsert_script_config.assert_not_called()

    async def test_transform_category_param_applied(self, transform_tools, mock_client):
        """The category param is honored on the transform path (was ignored)."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_handler("lighting")
        )

        result = await transform_tools.ha_config_set_script(
            script_id="test_script",
            python_transform="config['mode'] = 'single'",
            config_hash="prior_hash",
            category="lighting",
        )

        assert result["success"] is True
        assert result["category"] == "lighting"
        category_updates = [
            c[0][0]
            for c in mock_client.send_websocket_message.call_args_list
            if c[0][0].get("type") == "config/entity_registry/update"
        ]
        assert category_updates
        assert category_updates[0]["categories"] == {"script": "lighting"}

    @pytest.mark.parametrize("transform", [False, True])
    async def test_category_targets_renamed_entity(
        self,
        transform_tools,
        mock_client,
        sequence_config,
        transform,
        monkeypatch,
        registration_clock,
    ):
        """A registry-renamed script gets its category on the CURRENT entity.

        The storage key stays the registry unique_id after a rename, so the
        resolver must be asked for the entity_id instead of constructing
        ``script.<storage_key>``. Registration is delayed in both write modes.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_handler("lighting")
        )
        mock_client.upsert_script_config.return_value = {
            "success": True,
            "script_id": "storage_key",
        }
        registered = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_registered", registered
        )
        arguments = (
            {
                "python_transform": "config['mode'] = 'single'",
                "config_hash": "prior_hash",
            }
            if transform
            else {"config": sequence_config}
        )

        with patch(
            "ha_mcp.tools.entity_registration.fetch_entity_lookup_via_component",
            new_callable=AsyncMock,
            side_effect=[
                [],
                [],
                [],
                [
                    {
                        "entity_id": "script.renamed_alias",
                        "unique_id": "storage_key",
                        "platform": "script",
                    }
                ],
            ],
        ) as lookup:
            result = await transform_tools.ha_config_set_script(
                script_id="renamed_alias",
                category="lighting",
                **arguments,
            )

        assert result["success"] is True
        update_call = next(
            c[0][0]
            for c in mock_client.send_websocket_message.call_args_list
            if c[0][0].get("type") == "config/entity_registry/update"
        )
        assert update_call["entity_id"] == "script.renamed_alias"
        assert lookup.await_count == 4
        lookup.assert_awaited_with(mock_client, "storage_key", domain="script")
        registered.assert_awaited_once_with(mock_client, "script.renamed_alias")


class TestScriptEntityResolutionFallbacks:
    """The registry-scan branch carries the rename fix on installs without
    the component; the resolver is also skipped entirely when its result
    would go unused (wait=False, no category)."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_entity_state = AsyncMock(return_value={"state": "off"})
        client.upsert_script_config = AsyncMock(
            return_value={"success": True, "script_id": "test_script"}
        )
        client.get_services = AsyncMock(return_value=[])
        client.get_states = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def tools(self, mock_client):
        return ConfigScriptTools(mock_client)

    @pytest.fixture
    def transform_tools(self, tools):
        tools._fetch_and_verify_hash = AsyncMock(
            return_value=(
                {"alias": "Test Script", "sequence": [{"delay": {"seconds": 1}}]},
                "test_script",
            )
        )
        tools._get_script_config_internal = AsyncMock(
            return_value=({}, "newhash", None)
        )
        return tools

    async def test_registry_scan_resolves_rename_without_component(
        self, transform_tools, mock_client
    ):
        """Component unavailable (None): the registry-list unique_id scan
        must find the renamed entity, not fall to the constructed id."""

        async def handler(msg: dict) -> dict:
            msg_type = msg.get("type")
            if msg_type == "config/category_registry/list":
                return {"success": True, "result": [{"category_id": "lighting"}]}
            if msg_type == "config/entity_registry/get":
                return {"success": False, "error": "not found"}
            if msg_type == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "platform": "script",
                            "unique_id": "test_script",
                            "entity_id": "script.renamed_target",
                        }
                    ],
                }
            return {"success": True, "result": {"categories": {}}}

        mock_client.send_websocket_message = AsyncMock(side_effect=handler)

        with patch(
            "ha_mcp.tools.entity_registration.fetch_entity_lookup_via_component",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await transform_tools.ha_config_set_script(
                script_id="test_script",
                python_transform="config['mode'] = 'single'",
                config_hash="prior_hash",
                category="lighting",
            )

        assert result["success"] is True
        update_call = next(
            c[0][0]
            for c in mock_client.send_websocket_message.call_args_list
            if c[0][0].get("type") == "config/entity_registry/update"
        )
        assert update_call["entity_id"] == "script.renamed_target"

    async def test_no_resolution_when_result_unused(self, tools, mock_client):
        """wait=False without a category skips the resolver round-trips."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {}}
        )

        with patch(
            "ha_mcp.tools.entity_registration.fetch_entity_lookup_via_component",
            new_callable=AsyncMock,
        ) as lookup:
            result = await tools.ha_config_set_script(
                script_id="test_script",
                config={
                    "alias": "Test Script",
                    "sequence": [{"delay": {"seconds": 1}}],
                },
                wait=False,
            )

        assert result["success"] is True
        lookup.assert_not_called()
        # The gate must skip the raw registry fallbacks too, and this path
        # has no other expected WebSocket request (no category validation).
        mock_client.send_websocket_message.assert_not_called()
