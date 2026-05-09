"""Unit tests for Scene configuration tools.

Validates the input-validation gates and python_transform flow on the
scene CRUD tools (issue #995). Mirrors test_tools_config_scripts.py shape;
the key shape difference is that scene ``entities`` is a dict, not a list.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_config_scenes import ConfigSceneTools


@pytest.fixture
def mock_client():
    """Mock client that satisfies the upsert / get / reference-validator paths."""
    client = MagicMock()
    client.upsert_scene_config = AsyncMock(
        return_value={"success": True, "scene_id": "test_scene"}
    )
    client.get_scene_config = AsyncMock(
        return_value={
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
    )
    client.delete_scene_config = AsyncMock(
        return_value={"success": True, "scene_id": "test_scene"}
    )
    client.get_entity_state = AsyncMock(
        return_value={
            "state": "2026-05-08T07:00:00+00:00",  # scenes' state is the last-activated ISO timestamp
            "entity_id": "scene.test_scene",
        }
    )
    # validate_config_references reaches for these — keep them empty-but-callable.
    client.get_services = AsyncMock(return_value=[])
    client.get_states = AsyncMock(return_value=[])
    # Default registry list returns empty — _resolve_scene_entity_id falls
    # back to f"scene.{scene_id}". Individual tests override as needed.
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": []}
    )
    # rest_client.resolve_scene_id is consulted in the no-hash config-mode
    # path (issue #1168 R3 blocker 6, threading the storage key through to
    # responses). Default identity-mapping mirrors the real resolver's
    # fallback when the unique_id lookup misses; tests asserting a slug↔
    # storage-key remapping override per-test.
    client.resolve_scene_id = AsyncMock(
        side_effect=lambda sid: sid.removeprefix("scene.")
    )
    return client


@pytest.fixture
def tools(mock_client, monkeypatch):
    # Issue #1168 R3 blocker 1 added a 200 ms retry-sleep on registry-miss
    # for ``_resolve_scene_entity_id``. Patch to 0 in tests so the
    # fallback paths don't multiply unit-test wall-clock time.
    monkeypatch.setattr(ConfigSceneTools, "_RESOLVE_RETRY_DELAY", 0)
    return ConfigSceneTools(mock_client)


class TestSceneToolsValidation:
    """Validation gates on ha_config_set_scene."""

    async def test_set_scene_missing_entities_field(self, tools):
        """A config without 'entities' is rejected with a structured error."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"name": "No entities"},  # Missing 'entities'
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "entities" in error_data["error"]["message"].lower()

    async def test_set_scene_entities_must_be_dict_not_list(self, tools):
        """Scene shape: ``entities`` is a dict — list rejected (the script→scene confusion vector)."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={
                    "name": "Wrong shape",
                    # Common LLM-misroute: list-of-actions like an automation
                    "entities": [{"entity_id": "light.kitchen", "state": "on"}],
                },
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        msg = error_data["error"]["message"]
        assert "dict" in msg.lower() and "entities" in msg.lower()

    async def test_set_scene_with_entities_dict_succeeds(self, tools, mock_client):
        """Happy path: a dict-shaped 'entities' is accepted."""
        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={
                "name": "Test Scene",
                "entities": {
                    "light.kitchen": {"state": "on", "brightness": 200},
                },
            },
            wait=False,
        )

        assert result["success"] is True
        mock_client.upsert_scene_config.assert_called_once()

    async def test_set_scene_config_and_python_transform_mutex(self, tools):
        """Passing both config and python_transform is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"entities": {"light.kitchen": {"state": "on"}}},
                python_transform="config['entities'].clear()",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "both" in error_data["error"]["message"].lower()

    async def test_set_scene_neither_config_nor_transform(self, tools):
        """Calling with neither config nor python_transform is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(scene_id="test_scene")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    async def test_set_scene_python_transform_requires_config_hash(self, tools):
        """python_transform without config_hash is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="config['entities']['light.kitchen']['brightness'] = 100",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "config_hash" in error_data["error"]["message"]

    async def test_set_scene_invalid_json_string(self, tools):
        """A malformed JSON string for config surfaces a parse error."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config="{not valid json",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "config" in error_data["error"]["message"].lower()

    async def test_set_scene_config_must_be_object(self, tools):
        """A JSON-array config (not an object) is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config="[1, 2, 3]",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        msg = error_data["error"]["message"].lower()
        assert "object" in msg or "dict" in msg


class TestScenePythonTransform:
    """Coverage for the python_transform code path."""

    async def test_transform_updates_entity_state(self, tools, mock_client):
        """A successful transform routes through the upsert path."""
        # First call (hash verify) returns the seed config.
        # Second call (re-fetch authoritative hash) returns the same shape.
        seed = {
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on", "brightness": 100}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        # Compute the hash the tool will see on the verify step.
        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            python_transform=(
                "config['entities']['light.kitchen']['brightness'] = 200"
            ),
            config_hash=seed_hash,
        )

        assert result["success"] is True
        assert result["action"] == "python_transform"

        # The transform was applied before upsert: verify the brightness change.
        upsert_call_args = mock_client.upsert_scene_config.call_args
        config_passed = upsert_call_args[0][0]
        assert config_passed["entities"]["light.kitchen"]["brightness"] == 200

    async def test_transform_hash_mismatch_raises_conflict(self, tools, mock_client):
        """A stale config_hash on python_transform surfaces a conflict error."""
        seed = {
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="config['entities']['light.kitchen']['state'] = 'off'",
                config_hash="stale-hash-that-doesnt-match",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "modified" in error_data["error"]["message"].lower()

    async def test_transform_threads_category_through_to_apply(
        self, tools, mock_client, monkeypatch
    ):
        """G2 regression: python_transform branch must apply category, not silently
        drop it. Previously the branch returned early after upsert, bypassing
        apply_entity_category — surfaced in the PR #1168 Gemini review.
        """
        seed = {
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        # Stub the resolver so we don't depend on registry plumbing here.
        async def _stub_resolve(scene_id):
            return f"scene.{scene_id}"

        monkeypatch.setattr(tools, "_resolve_scene_entity_id", _stub_resolve)

        # Capture apply_entity_category invocations from the tools module.
        calls: list[tuple] = []

        async def _capture_category(*args, **kwargs):
            calls.append((args, kwargs))
            return None

        from ha_mcp.tools import tools_config_scenes as scene_mod

        monkeypatch.setattr(scene_mod, "apply_entity_category", _capture_category)

        # Also stub wait_for_entity_registered so we don't sleep in tests.
        async def _stub_wait(*_args, **_kwargs):
            return True

        monkeypatch.setattr(scene_mod, "wait_for_entity_registered", _stub_wait)

        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            python_transform=(
                "config['entities']['light.kitchen']['brightness'] = 100"
            ),
            config_hash=seed_hash,
            category="my_category",
        )

        assert result["success"] is True
        assert result["action"] == "python_transform"
        # apply_entity_category must have been invoked with the category
        # passed to the tool, not silently dropped.
        assert len(calls) == 1, (
            f"apply_entity_category should be invoked once on python_transform "
            f"branch when category is set; calls={calls}"
        )
        invoked_args = calls[0][0]
        assert "my_category" in invoked_args, (
            f"category arg should be threaded through; got args={invoked_args}"
        )

    async def test_transform_must_keep_entities_dict_shape(self, tools, mock_client):
        """A transform that drops the entities key fails the post-transform check."""
        seed = {
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="del config['entities']",
                config_hash=seed_hash,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "entities" in error_data["error"]["message"].lower()

    async def test_transform_disallowed_import_surfaces_tool_error(
        self, tools, mock_client
    ):
        """Imports are not in ``SAFE_NODES`` → PythonSandboxError → ToolError.

        Mirrors ``test_python_transform_blocked_import`` in the script
        suite. Locks the contract that import-attempts (the canonical
        sandbox-bypass attempt) surface as VALIDATION_FAILED with a
        message naming the offending node, not as a stack trace or
        SERVICE_CALL_FAILED.
        """
        seed = {"name": "S", "entities": {"light.kitchen": {"state": "on"}}}
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="import os; os.system('echo pwned')",
                config_hash=seed_hash,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_FAILED"
        # Message should name the failure mode somewhere — the python_sandbox
        # surfaces "Import" as the forbidden node type.
        msg_lower = error_data["error"]["message"].lower()
        assert "import" in msg_lower or "forbidden" in msg_lower

    async def test_transform_syntax_error_surfaces_tool_error(self, tools, mock_client):
        """A python_transform that fails to compile (syntax error) surfaces
        as ToolError VALIDATION_FAILED, not an unhandled SyntaxError leaking
        a stack trace through the tool boundary.
        """
        seed = {"name": "S", "entities": {"light.kitchen": {"state": "on"}}}
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                # Unmatched bracket — clean syntax error in expression.
                python_transform="config['entities']['light.kitchen'",
                config_hash=seed_hash,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_FAILED"

    async def test_transform_runtime_keyerror_surfaces_tool_error(
        self, tools, mock_client
    ):
        """Runtime exceptions (KeyError, NameError, ZeroDivisionError, …)
        raised by valid-but-failing transform expressions surface as
        ToolError VALIDATION_FAILED rather than propagating raw.
        """
        seed = {"name": "S", "entities": {"light.kitchen": {"state": "on"}}}
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                # Valid syntax, valid AST — but the key doesn't exist at runtime.
                python_transform="del config['entities']['light.nonexistent']",
                config_hash=seed_hash,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_FAILED"


class TestResolveSceneEntityId:
    """Coverage for _resolve_scene_entity_id — the BAT-discovered fix.

    HA derives a scene's entity_id from its ``name`` slug, not from the
    ``scene_id`` storage key, so a naive ``f"scene.{scene_id}"`` returns
    a non-existent entity_id whenever the user supplies a name. The
    resolver must look up the entity registry by ``unique_id`` and
    return the actual entity_id.
    """

    async def test_resolver_returns_actual_entity_id_when_name_differs(
        self, tools, mock_client
    ):
        """Registry has a scene with unique_id matching scene_id but a
        different entity_id slug — resolver returns the registry's slug."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "entity_id": "scene.led_desk_strip_night_light",
                        "unique_id": "night_light_led_desk_strip",
                        "platform": "homeassistant",
                    }
                ],
            }
        )

        result = await tools._resolve_scene_entity_id("night_light_led_desk_strip")

        assert result == "scene.led_desk_strip_night_light"

    async def test_resolver_falls_back_to_naive_when_registry_misses(
        self, tools, mock_client
    ):
        """If no registry entry matches the scene_id, fall back to the
        naive ``scene.<scene_id>`` form rather than raising."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "entity_id": "scene.unrelated",
                        "unique_id": "unrelated",
                        "platform": "homeassistant",
                    }
                ],
            }
        )

        result = await tools._resolve_scene_entity_id("test_scene")

        assert result == "scene.test_scene"

    async def test_resolver_ignores_non_scene_entity_with_matching_unique_id(
        self, tools, mock_client
    ):
        """A registry entry from a different domain that happens to share
        the same unique_id must NOT be returned — only entries under the
        ``scene.*`` domain."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "entity_id": "automation.collision",
                        "unique_id": "test_scene",
                        "platform": "homeassistant",
                    }
                ],
            }
        )

        result = await tools._resolve_scene_entity_id("test_scene")

        assert result == "scene.test_scene"

    async def test_resolver_falls_back_on_ha_api_failure(self, tools, mock_client):
        """HA-API connection exception → naive fallback, not propagated.

        The resolver narrows its `except` to HA-API failure types so the
        caller still gets a best-effort entity_id when the registry
        lookup itself is the problem. Programming bugs (AttributeError,
        KeyError, …) are intentionally NOT caught — see the companion
        ``test_resolver_does_not_swallow_programming_bugs`` test.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantConnectionError("WS unavailable")
        )

        result = await tools._resolve_scene_entity_id("test_scene")

        assert result == "scene.test_scene"

    async def test_resolver_falls_back_on_api_error(self, tools, mock_client):
        """HomeAssistantAPIError also routes through the narrow fallback path."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantAPIError("registry list failed")
        )

        result = await tools._resolve_scene_entity_id("test_scene")

        assert result == "scene.test_scene"

    async def test_resolver_falls_back_on_timeout(self, tools, mock_client):
        """asyncio TimeoutError (== built-in TimeoutError on 3.11+) routes
        through the narrow fallback path."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=TimeoutError("registry list timed out")
        )

        result = await tools._resolve_scene_entity_id("test_scene")

        assert result == "scene.test_scene"

    async def test_resolver_does_not_swallow_programming_bugs(self, tools, mock_client):
        """Generic Exception (e.g. an AttributeError from a misnamed
        registry-result key) must propagate — the narrow catch was
        introduced specifically so cosmetic 'not yet queryable' warnings
        don't mask real bugs further up the call chain."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=AttributeError("dict has no attribute 'foo'")
        )

        with pytest.raises(AttributeError, match="no attribute 'foo'"):
            await tools._resolve_scene_entity_id("test_scene")

    async def test_resolver_strips_scene_prefix(self, tools, mock_client):
        """Passing a fully-qualified ``scene.foo`` must not yield
        ``scene.scene.foo`` on fallback. Mirrors
        ``rest_client.resolve_scene_id`` ergonomics."""
        # Force fallback by making registry list yield no match.
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        result = await tools._resolve_scene_entity_id("scene.movie_night")

        assert result == "scene.movie_night"

    async def test_resolver_retries_once_when_first_call_misses(
        self, tools, mock_client
    ):
        """Issue #1168 R3 blocker 1: on a freshly-upserted scene the
        registry index lags the storage write by tens to ~200 ms. The
        resolver must retry once after a short delay before falling back
        to the naive entity_id, otherwise post-upsert callsites trail
        ``wait_for_entity_registered`` to a phantom-404 timeout. First
        call returns no match, second call returns the resolved entity
        — resolver returns the registry's slug, not ``scene.<scene_id>``.
        """
        responses = [
            {"success": True, "result": []},
            {
                "success": True,
                "result": [
                    {
                        "entity_id": "scene.led_desk_strip_night_light",
                        "unique_id": "night_light_led_desk_strip",
                        "platform": "homeassistant",
                    }
                ],
            },
        ]
        mock_client.send_websocket_message = AsyncMock(side_effect=responses)

        result = await tools._resolve_scene_entity_id("night_light_led_desk_strip")

        assert result == "scene.led_desk_strip_night_light"
        assert mock_client.send_websocket_message.call_count == 2

    async def test_resolver_skips_retry_on_api_failure(self, tools, mock_client):
        """Issue #1168 R3 blocker 1: API-level failures are sticky (auth /
        500 / connection won't change between calls), so the resolver must
        bail to the naive form immediately instead of double-paying the
        timeout. Single call expected; result is the naive fallback."""
        from ha_mcp.client.rest_client import HomeAssistantConnectionError

        mock_client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantConnectionError("registry offline")
        )

        result = await tools._resolve_scene_entity_id("test_scene")

        assert result == "scene.test_scene"
        assert mock_client.send_websocket_message.call_count == 1


@pytest.mark.asyncio
class TestSceneRestClientErrorMapping:
    """Tool-level mapping of rest_client errors to structured ToolError responses.

    Locks the contract that 404 from the REST client surfaces as
    ``ENTITY_NOT_FOUND`` (so agents can branch on missing-scene cleanly), and
    that other API errors (notably 400 for malformed configs) surface with
    enough context to act on. The rest_client layer's own `raise` paths are
    exercised in test_rest_client_scenes; these tests cover the tool-side
    `exception_to_structured_error` mapping.
    """

    @pytest.fixture
    def tools(self, mock_client):
        return ConfigSceneTools(mock_client)

    async def test_get_scene_404_surfaces_as_entity_not_found(self, tools, mock_client):
        """get_scene_config raising 404 → tool ToolError with code
        ENTITY_NOT_FOUND, entity_id surfaced as ``scene.<id>``.

        The tool's catch site passes ``entity_id`` alongside ``scene_id``
        in context; the helper's classifier branches on ``entity_id``-
        presence to pick ENTITY_NOT_FOUND over the generic
        RESOURCE_NOT_FOUND. Without the entity_id passthrough the agent
        loses the scenes-are-entities signal and retries via the wrong
        lookup path.
        """
        mock_client.get_scene_config = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "Scene not found: missing_scene", status_code=404
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_scene(scene_id="missing_scene")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "ENTITY_NOT_FOUND"
        # entity_id must be in the flattened context so the agent's
        # retry/lookup logic has a clean handle on what was missing.
        assert error_data.get("entity_id") == "scene.missing_scene"

    async def test_set_scene_400_surfaces_with_scene_id_context(
        self, tools, mock_client
    ):
        """upsert_scene_config raising 400 (malformed config) surfaces as
        a structured tool error with scene_id in context — the highest-
        likelihood scene failure mode (LLM submits invalid entity state).

        Also asserts the entities-shape suggestion is present so a small
        model has the actionable hint without re-reading the docstring.
        """
        mock_client.get_scene_config = AsyncMock(
            return_value={"name": "X", "entities": {"light.kitchen": {"state": "on"}}}
        )
        mock_client.upsert_scene_config = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "Invalid entity state: 'bogus_state' for light.kitchen",
                status_code=400,
            )
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="bad_scene",
                config={
                    "name": "Bad Scene",
                    "entities": {"light.kitchen": {"state": "bogus_state"}},
                },
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        # The helper maps HTTP 400 → ErrorCode.VALIDATION_FAILED via
        # create_validation_error (errors.py:334). Assert the code
        # explicitly so a regression remapping 400 to a generic code
        # (e.g. INTERNAL_ERROR) is caught loud.
        assert error_data["error"]["code"] == "VALIDATION_FAILED", (
            f"400 must map to VALIDATION_FAILED, got: {error_data['error'].get('code')}"
        )
        assert error_data.get("scene_id") == "bad_scene"
        suggestions = error_data["error"].get("suggestions") or []
        # The helper-doc mentions both shape and ha_search hint; either
        # signal that the agent has actionable guidance.
        assert any(
            "entities" in (s or "").lower() or "scene shape" in (s or "").lower()
            for s in suggestions
        ), f"Expected entities-shape hint in suggestions, got: {suggestions}"


@pytest.mark.asyncio
class TestSceneResponseShape:
    """Issue #1168 R3 blockers 3, 4, 6: response-shape contracts.

    Locks the unwrapped get-config payload, the absence of the
    misleading ``operation`` field on upsert, and the storage-key
    consistency across get / set / remove / conflict responses
    regardless of whether the caller passed the entity_id slug or
    the storage key.
    """

    async def test_get_scene_response_is_not_doubly_nested(
        self, tools, mock_client
    ):
        """``ha_config_get_scene`` returns the actual scene body in
        ``response['config']`` — no nested ``success`` / ``scene_id`` /
        ``config`` from the rest-client envelope."""
        mock_client.get_scene_config = AsyncMock(
            return_value={
                "success": True,
                "scene_id": "movie_night",
                "config": {
                    "id": "movie_night",
                    "name": "Movie Night",
                    "entities": {"light.tv": {"state": "on"}},
                },
            }
        )

        result = await tools.ha_config_get_scene(scene_id="movie_night")

        # Outer envelope: success / action / scene_id / config / config_hash.
        assert result["success"] is True
        assert result["action"] == "get"
        assert result["scene_id"] == "movie_night"
        # ``config`` carries the scene body directly — not the wrapper.
        assert "success" not in result["config"]
        assert result["config"].get("id") == "movie_night"
        assert result["config"].get("entities") == {"light.tv": {"state": "on"}}

    async def test_set_scene_response_omits_misleading_operation_field(
        self, tools, mock_client
    ):
        """Issue #1168 R3 blocker 4: HA's POST returns the same ``"ok"``
        for create and update, so the rest_client used to report
        ``operation: "created"`` for every successful upsert. The field
        was dropped (it was a tautology); the response must not contain
        ``operation``."""
        # rest_client.upsert_scene_config no longer emits ``operation``.
        mock_client.upsert_scene_config = AsyncMock(
            return_value={
                "success": True,
                "scene_id": "movie_night",
                "result": "ok",
            }
        )

        result = await tools.ha_config_set_scene(
            scene_id="movie_night",
            config={"name": "Movie Night", "entities": {"light.x": {"state": "on"}}},
            wait=False,
        )

        assert result["success"] is True
        assert "operation" not in result, (
            f"`operation` field was dropped (R3 blocker 4); got: {result}"
        )

    async def test_set_scene_response_uses_storage_key_when_caller_passes_slug(
        self, tools, mock_client
    ):
        """Issue #1168 R3 blocker 6: the outer ``scene_id`` is the
        rest-client-resolved storage key, regardless of whether the caller
        passed the entity_id slug. Caller passes "scene.led_desk_strip" but
        the resolver returns "night_light_led_desk_strip" (rename history)."""
        mock_client.resolve_scene_id = AsyncMock(return_value="night_light_led_desk_strip")
        mock_client.upsert_scene_config = AsyncMock(
            return_value={
                "success": True,
                "scene_id": "night_light_led_desk_strip",
                "result": "ok",
            }
        )

        result = await tools.ha_config_set_scene(
            scene_id="led_desk_strip",
            config={"name": "X", "entities": {"light.x": {"state": "on"}}},
            wait=False,
        )

        assert result["scene_id"] == "night_light_led_desk_strip"

    async def test_remove_scene_response_uses_storage_key(
        self, tools, mock_client
    ):
        """Issue #1168 R3 blocker 6: remove-scene response surfaces the
        storage key, even when the caller passed the entity_id slug."""
        mock_client.resolve_scene_id = AsyncMock(return_value="night_light_led_desk_strip")
        mock_client.delete_scene_config = AsyncMock(
            return_value={
                "success": True,
                "scene_id": "night_light_led_desk_strip",
                "result": "ok",
            }
        )

        result = await tools.ha_config_remove_scene(
            scene_id="led_desk_strip", wait=False,
        )

        assert result["scene_id"] == "night_light_led_desk_strip"

    async def test_set_scene_config_mode_stale_hash_raises_conflict(
        self, tools, mock_client
    ):
        """Issue #1168 R3 blocker 7: when the caller passes
        ``config_hash`` in config-mode (full replacement), the tool
        verifies it before upsert. A stale hash surfaces as a structured
        conflict error AND carries the fresh hash in context (per the
        ``current_config_hash`` contract from R1).
        """
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Old Name",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        # rest_client.get_scene_config returns the rest-client envelope.
        mock_client.get_scene_config = AsyncMock(
            return_value={"success": True, "scene_id": "test_scene", "config": seed}
        )
        fresh_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={
                    "name": "New Name",
                    "entities": {"light.kitchen": {"state": "off"}},
                },
                config_hash="stale-hash-value",
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "modified" in error_data["error"]["message"].lower()
        # Fresh hash carried in context so the caller can retry.
        assert error_data.get("current_config_hash") == fresh_hash
        # upsert was NOT called — the hash check fires first.
        mock_client.upsert_scene_config.assert_not_called()

    async def test_set_scene_config_mode_matching_hash_proceeds(
        self, tools, mock_client
    ):
        """Issue #1168 R3 blocker 7: matching ``config_hash`` lets the
        upsert proceed (no conflict). Sanity check that the stale-hash
        path isn't blocking the legitimate matching-hash flow."""
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Old Name",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(
            return_value={"success": True, "scene_id": "test_scene", "config": seed}
        )
        fresh_hash = compute_config_hash(seed)

        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={
                "name": "New Name",
                "entities": {"light.kitchen": {"state": "off"}},
            },
            config_hash=fresh_hash,
            wait=False,
        )

        assert result["success"] is True
        mock_client.upsert_scene_config.assert_called_once()


@pytest.mark.asyncio
class TestPythonTransformOrphanMetadata:
    """Issue #1168 R3 blocker 5: a python_transform comprehension that
    filters ``entities`` leaves ``metadata`` orphan entries on disk
    because HA's storage write doesn't reconcile the two dicts. The
    tool prunes orphan keys before upsert so the contract matches the
    full-replace ``config=`` mode (which clears metadata cleanly).
    """

    async def test_orphan_metadata_pruned_after_entity_filter(
        self, tools, mock_client
    ):
        """A list-comprehension transform filters ``entities`` — metadata
        for the removed entity is pruned before upsert, mirroring full-
        replace's behaviour."""
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": {
                "light.kitchen": {"state": "on"},
                "select.motion2_baud_rate": {"state": "9600"},
            },
            "metadata": {
                "light.kitchen": {"entity_only": True},
                "select.motion2_baud_rate": {"entity_only": True},
            },
        }
        mock_client.get_scene_config = AsyncMock(
            return_value={"success": True, "scene_id": "test_scene", "config": seed}
        )
        seed_hash = compute_config_hash(seed)

        # Transform: keep only the kitchen light.
        await tools.ha_config_set_scene(
            scene_id="test_scene",
            python_transform=(
                "config['entities'] = "
                "{k: v for k, v in config['entities'].items() "
                "if k == 'light.kitchen'}"
            ),
            config_hash=seed_hash,
            wait=False,
        )

        # Inspect the config dict the tool actually sent to upsert.
        upsert_call = mock_client.upsert_scene_config.call_args
        sent_config = upsert_call.args[0]
        assert sent_config["entities"] == {"light.kitchen": {"state": "on"}}
        # Orphan metadata for the filtered-out entity must be gone.
        assert sent_config["metadata"] == {
            "light.kitchen": {"entity_only": True}
        }
        assert "select.motion2_baud_rate" not in sent_config["metadata"]

    async def test_metadata_unchanged_when_entities_unchanged(
        self, tools, mock_client
    ):
        """The prune step must not corrupt metadata when the transform
        doesn't touch ``entities`` (e.g., a brightness adjustment on an
        existing entity). Both pre- and post-transform metadata identical.
        """
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on", "brightness": 100}},
            "metadata": {"light.kitchen": {"entity_only": True}},
        }
        mock_client.get_scene_config = AsyncMock(
            return_value={"success": True, "scene_id": "test_scene", "config": seed}
        )
        seed_hash = compute_config_hash(seed)

        await tools.ha_config_set_scene(
            scene_id="test_scene",
            python_transform=(
                "config['entities']['light.kitchen']['brightness'] = 200"
            ),
            config_hash=seed_hash,
            wait=False,
        )

        sent_config = mock_client.upsert_scene_config.call_args.args[0]
        assert sent_config["metadata"] == {
            "light.kitchen": {"entity_only": True}
        }
