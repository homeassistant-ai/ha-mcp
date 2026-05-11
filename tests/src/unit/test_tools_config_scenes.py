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
            "id": "test_scene",
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
            "id": "test_scene",
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
            "id": "test_scene",
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

        # B9: _validate_category_id pre-flights against the category registry.
        # Wire send_websocket_message to recognise the list call and surface
        # ``my_category`` so validation passes; default empty result for
        # everything else (resolver, etc.) is unaffected because the resolver
        # is stubbed above.
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"category_id": "my_category", "name": "My Cat"}],
            }
        )

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
            "id": "test_scene",
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
        seed = {
            "id": "test_scene",
            "name": "S",
            "entities": {"light.kitchen": {"state": "on"}},
        }
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
        seed = {
            "id": "test_scene",
            "name": "S",
            "entities": {"light.kitchen": {"state": "on"}},
        }
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
        seed = {
            "id": "test_scene",
            "name": "S",
            "entities": {"light.kitchen": {"state": "on"}},
        }
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

    async def test_transform_backslash_escape_surfaces_specific_hint(
        self, tools, mock_client
    ):
        """A python_transform with ``\\"`` outside a string literal trips
        Python's "unexpected character after line continuation character"
        parser error. The scene tool routes that through
        ``format_sandbox_error``, so the response must carry the
        backslash-specific hint (not just a generic "Check expression
        syntax") and an ``ErrorCode.VALIDATION_FAILED``.
        """
        seed = {
            "id": "test_scene",
            "name": "S",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)

        from ha_mcp.utils.config_hash import compute_config_hash

        seed_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                # The agent intended a JSON-style quote escape; outside a
                # Python string literal the leading ``\`` is a
                # line-continuation token followed by an unexpected char.
                python_transform='config["entities"]["light.kitchen"] = \\"v\\"',
                config_hash=seed_hash,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_FAILED"
        suggestions = error_data["error"].get("suggestions", [])
        assert any("backslash" in s.lower() for s in suggestions), (
            f"Expected a backslash-escape hint among suggestions, got {suggestions!r}"
        )


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

    async def test_get_scene_response_is_not_doubly_nested(self, tools, mock_client):
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
        mock_client.resolve_scene_id = AsyncMock(
            return_value="night_light_led_desk_strip"
        )
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

    async def test_remove_scene_response_uses_storage_key(self, tools, mock_client):
        """Issue #1168 R3 blocker 6: remove-scene response surfaces the
        storage key, even when the caller passed the entity_id slug."""
        mock_client.resolve_scene_id = AsyncMock(
            return_value="night_light_led_desk_strip"
        )
        mock_client.delete_scene_config = AsyncMock(
            return_value={
                "success": True,
                "scene_id": "night_light_led_desk_strip",
                "result": "ok",
            }
        )

        result = await tools.ha_config_remove_scene(
            scene_id="led_desk_strip",
            wait=False,
        )

        assert result["scene_id"] == "night_light_led_desk_strip"

    async def test_set_scene_config_mode_stale_hash_raises_conflict(
        self, tools, mock_client
    ):
        """Issue #1168 R3 blocker 7 + R5 test gap 14: when the caller
        passes ``config_hash`` in config-mode (full replacement), the
        tool verifies it before upsert. A stale hash surfaces as a
        structured conflict error AND carries the fresh hash in
        context (per the ``current_config_hash`` contract from R1).

        R5 test gap 14: assert ``error_data["scene_id"]`` equals the
        resolved storage key (not the caller-input slug). Uses a
        slug-input fixture (``led_desk_strip`` → registry storage key
        ``night_light_led_desk_strip``) so a regression that drops the
        slug→storage-key thread-through is caught — without the slug
        remap, the caller-input value happens to match the storage key
        and the assertion is meaningless.
        """
        from ha_mcp.utils.config_hash import compute_config_hash

        # Slug → storage key remap, mirroring TestResolveSceneEntityId's
        # ``night_light_led_desk_strip`` fixture pattern.
        mock_client.resolve_scene_id = AsyncMock(
            return_value="night_light_led_desk_strip"
        )
        seed = {
            "id": "night_light_led_desk_strip",
            "name": "Old Name",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        # rest_client.get_scene_config returns the rest-client envelope.
        mock_client.get_scene_config = AsyncMock(
            return_value={
                "success": True,
                "scene_id": "night_light_led_desk_strip",
                "config": seed,
            }
        )
        fresh_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="led_desk_strip",  # input slug, not storage key
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
        # R5 test gap 14: storage key threaded through to error context.
        assert error_data.get("scene_id") == "night_light_led_desk_strip", (
            "Conflict error must carry the resolved storage key, not the "
            "caller-input slug — regression-guards the slug→storage-key "
            "thread-through introduced for R3 blocker 6."
        )
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

    async def test_orphan_metadata_pruned_after_entity_filter(self, tools, mock_client):
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
        assert sent_config["metadata"] == {"light.kitchen": {"entity_only": True}}
        assert "select.motion2_baud_rate" not in sent_config["metadata"]

    async def test_metadata_unchanged_when_entities_unchanged(self, tools, mock_client):
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
        assert sent_config["metadata"] == {"light.kitchen": {"entity_only": True}}


class TestPythonTransformGuardrails:
    """R5 blockers 8/10/12/13 — python_transform input/output guards."""

    async def test_transform_rebinding_config_to_none_rejected(
        self, tools, mock_client
    ):
        """R5 blocker 8: ``config = None`` inside the transform used to
        crash the next ``in`` check with a TypeError surfacing as
        ``INTERNAL_ERROR``. Reject with a clean ``VALIDATION_FAILED``
        signal instead.
        """
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)
        seed_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="config = None",
                config_hash=seed_hash,
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_FAILED"
        assert "none" in error_data["error"]["message"].lower()
        # Critical: the failure is NOT an INTERNAL_ERROR leak.
        assert error_data["error"]["code"] != "INTERNAL_ERROR"
        mock_client.upsert_scene_config.assert_not_called()

    async def test_transform_mutating_config_id_rejected(self, tools, mock_client):
        """R5 blocker 10: ``config['id'] = 'other'`` would create a
        duplicate scene at the new storage key and orphan the original.
        Reject before upsert reaches HA.
        """
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)
        seed_hash = compute_config_hash(seed)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform="config['id'] = 'completely_different_id'",
                config_hash=seed_hash,
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_FAILED"
        msg = error_data["error"]["message"]
        assert "id" in msg.lower()
        # The attempted-id is surfaced for the user.
        assert error_data.get("attempted_id") == "completely_different_id"
        # Upsert MUST NOT be called — duplicate prevention is the point.
        mock_client.upsert_scene_config.assert_not_called()

    async def test_transform_metadata_prune_logs_dropped_keys(
        self, tools, mock_client, caplog, monkeypatch
    ):
        """R5 blocker 12: metadata prune used to be silent, masking
        accidental entity drops. The prune step now logs at INFO with
        the list of pruned keys.
        """
        import logging

        # Stub apply_entity_category and wait so the test stays narrow.
        from ha_mcp.tools import tools_config_scenes as scene_mod
        from ha_mcp.utils.config_hash import compute_config_hash

        async def _noop_category(*_a, **_k):
            return None

        async def _stub_wait(*_a, **_k):
            return True

        monkeypatch.setattr(scene_mod, "apply_entity_category", _noop_category)
        monkeypatch.setattr(scene_mod, "wait_for_entity_registered", _stub_wait)

        seed = {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": {
                "light.kitchen": {"state": "on"},
                "light.bedroom": {"state": "on"},
            },
            "metadata": {
                "light.kitchen": {"entity_only": True},
                "light.bedroom": {"entity_only": True},
            },
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)
        seed_hash = compute_config_hash(seed)

        caplog.set_level(logging.INFO, logger="ha_mcp.tools.tools_config_scenes")

        await tools.ha_config_set_scene(
            scene_id="test_scene",
            python_transform="del config['entities']['light.bedroom']",
            config_hash=seed_hash,
            wait=False,
        )

        # The INFO log records BOTH the count and the specific key list.
        prune_records = [
            r
            for r in caplog.records
            if "pruned" in r.message and "metadata" in r.message
        ]
        assert prune_records, (
            f"metadata prune must log at INFO; got records={[r.message for r in caplog.records]}"
        )
        msg = prune_records[0].message
        assert "light.bedroom" in msg
        assert "1 orphan" in msg

    async def test_resolver_logs_debug_on_exhausted_retry(
        self, tools, mock_client, caplog
    ):
        """R5 blocker 13: when both registry calls succeed but neither
        matches, log at DEBUG before falling back to ``scene.<scene_id>``
        so the downstream phantom-404 warning has a correlative entry.
        """
        import logging

        # Registry list always returns no matching row.
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

        caplog.set_level(logging.DEBUG, logger="ha_mcp.tools.tools_config_scenes")

        result = await tools._resolve_scene_entity_id("missing_scene")

        # Naive fallback returned.
        assert result == "scene.missing_scene"
        # Exhausted-retry log entry present.
        exhaust_records = [
            r for r in caplog.records if "registry retry exhausted" in r.message.lower()
        ]
        assert exhaust_records, (
            f"exhausted-retry-no-match path must log at DEBUG; "
            f"got records={[r.message for r in caplog.records]}"
        )
        assert "missing_scene" in exhaust_records[0].message


class TestCategoryValidation:
    """R5 blocker 9 — pre-validate category IDs against the registry."""

    @staticmethod
    def _ws_side_effect_with_categories(category_ids: list[str]):
        """Build a ``send_websocket_message`` side_effect that returns
        the supplied category IDs for ``category_registry/list`` and an
        empty result for anything else (resolver registry calls etc.).
        """

        async def _side_effect(message):
            if isinstance(message, dict) and message.get("type") == (
                "config/category_registry/list"
            ):
                return {
                    "success": True,
                    "result": [{"category_id": cid} for cid in category_ids],
                }
            return {"success": True, "result": []}

        return _side_effect

    async def test_set_scene_config_mode_rejects_phantom_category(
        self, tools, mock_client
    ):
        """R5 blocker 9 (config-mode branch): a category ID that does
        not exist in the live registry is rejected with
        ``VALIDATION_INVALID_PARAMETER`` before HA writes it. Without
        this gate, HA accepts the phantom ID silently and the entity
        registry ends up with a reference invisible in
        ``ha_config_get_category(scope='scene')`` results.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_side_effect_with_categories(["lighting", "ambience"])
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={"name": "X", "entities": {"light.kitchen": {"state": "on"}}},
                category="nonexistent_id",
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "nonexistent_id" in error_data["error"]["message"]
        # Suggestion points at the right tool for category creation.
        suggestions_blob = " ".join(error_data["error"].get("suggestions") or [])
        assert "ha_config_set_category" in suggestions_blob
        # R6 blocker 20: assert the upsert was NOT called — without this the
        # R5 ordering bug (validation after upsert) was unit-invisible.
        mock_client.upsert_scene_config.assert_not_called()
        # R6 while-you're-in: validation must use ``scope=scene``. A regression
        # that drops the scope filter would silently accept any category.
        category_list_calls = [
            call.args[0]
            for call in mock_client.send_websocket_message.call_args_list
            if isinstance(call.args[0], dict)
            and call.args[0].get("type") == "config/category_registry/list"
        ]
        assert category_list_calls, (
            "_validate_category_id must hit config/category_registry/list"
        )
        assert all(c.get("scope") == "scene" for c in category_list_calls), (
            f"category_registry/list payload must carry scope='scene'; "
            f"saw {category_list_calls}"
        )

    async def test_set_scene_config_mode_accepts_existing_category(
        self, tools, mock_client
    ):
        """Sanity: an existing category ID passes the gate and the
        upsert proceeds.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_side_effect_with_categories(["lighting"])
        )

        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={"name": "X", "entities": {"light.kitchen": {"state": "on"}}},
            category="lighting",
            wait=False,
        )

        assert result["success"] is True
        # R6 while-you're-in: the happy-path upsert fires exactly once. A
        # regression that double-calls or skips the upsert when the category
        # gate passes would otherwise slip through.
        mock_client.upsert_scene_config.assert_called_once()

    async def test_python_transform_branch_rejects_phantom_category(
        self, tools, mock_client
    ):
        """R5 blocker 9 (python_transform branch): same gate must fire
        on the transform path too — the bug originally surfaced through
        the python_transform branch silently accepting phantom IDs.
        """
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)
        seed_hash = compute_config_hash(seed)
        # Live registry has only 'lighting' — phantom_id should be rejected.
        mock_client.send_websocket_message = AsyncMock(
            side_effect=self._ws_side_effect_with_categories(["lighting"])
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                python_transform=(
                    "config['entities']['light.kitchen']['brightness'] = 100"
                ),
                config_hash=seed_hash,
                category="phantom_id",
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "phantom_id" in error_data["error"]["message"]
        # R6 blocker 20: assert the upsert was NOT called — the R5 ordering
        # bug let a phantom-category transform write through and only error
        # afterwards.
        mock_client.upsert_scene_config.assert_not_called()


class TestEmptySceneIdGuard:
    """R6 blocker 16 — empty ``scene_id`` pre-flight on all three tools.

    Previously ``set_scene("", …)`` returned ``RESOURCE_NOT_FOUND`` with a
    misleading ``entities``-related suggestion (the downstream lookup raised
    on the empty key). The pre-flight surfaces the actual problem.
    """

    async def test_get_scene_rejects_empty_scene_id(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_scene(scene_id="")

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene_id must not be empty" in body["error"]["message"]

    async def test_set_scene_rejects_empty_scene_id(self, tools, mock_client):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="",
                config={"name": "X", "entities": {"light.x": {"state": "on"}}},
                wait=False,
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene_id must not be empty" in body["error"]["message"]
        # Critical: the guard fires BEFORE upsert reaches HA.
        mock_client.upsert_scene_config.assert_not_called()

    async def test_remove_scene_rejects_empty_scene_id(self, tools, mock_client):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_scene(scene_id="", wait=False)

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene_id must not be empty" in body["error"]["message"]
        mock_client.delete_scene_config.assert_not_called()

    @pytest.mark.parametrize("whitespace_value", ["   ", "\t", "\n", " \t\n "])
    async def test_get_scene_rejects_whitespace_only_scene_id(
        self,
        tools,
        whitespace_value: str,
    ):
        """R7 blocker 23: whitespace-only ``scene_id`` slipped past the
        original ``if not scene_id`` and surfaced as ``RESOURCE_NOT_FOUND`` —
        the exact misleading error B16 was supposed to prevent. Refined
        guard catches it via ``not scene_id.strip()``."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_scene(scene_id=whitespace_value)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene_id must not be empty" in body["error"]["message"]

    async def test_set_scene_rejects_whitespace_only_scene_id(
        self,
        tools,
        mock_client,
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="   ",
                config={"name": "X", "entities": {"light.x": {"state": "on"}}},
                wait=False,
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene_id must not be empty" in body["error"]["message"]
        mock_client.upsert_scene_config.assert_not_called()

    async def test_remove_scene_rejects_whitespace_only_scene_id(
        self,
        tools,
        mock_client,
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_scene(scene_id="   ", wait=False)
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene_id must not be empty" in body["error"]["message"]
        mock_client.delete_scene_config.assert_not_called()


class TestPythonTransformDeleteIdLegitimate:
    """R6 blocker 18 — a transform that ``del config['id']`` is legitimate
    (HA treats ``id`` as optional). The R5 strict check rejected this; the
    R6 refinement only blocks an EXPLICIT mismatched id.
    """

    async def test_transform_deleting_id_passes_through(self, tools, mock_client):
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)
        seed_hash = compute_config_hash(seed)

        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            python_transform="del config['id']",
            config_hash=seed_hash,
            wait=False,
        )

        assert result["success"] is True
        assert result["action"] == "python_transform"
        # The upsert was called — id-deletion is legitimate.
        mock_client.upsert_scene_config.assert_called_once()
        sent_config = mock_client.upsert_scene_config.call_args.args[0]
        assert "id" not in sent_config


class TestSceneRegistryFallbackClean:
    """R6 while-you're-in: when the registry fetch fails BUT per-id config
    fetches succeed (B11 fallback engages and recovers), the response must
    NOT carry ``partial: true``. Today only the "registry fails AND per-id
    fails" path is covered; this locks the clean-fallback case.
    """

    async def test_registry_fail_per_id_succeed_no_partial(self) -> None:
        from typing import Any
        from unittest.mock import MagicMock as _MagicMock
        from unittest.mock import patch as _patch

        from ha_mcp.tools.smart_search import SmartSearchTools

        client = _MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "scene.bedroom",
                    "state": "scening",
                    "attributes": {"friendly_name": "Bedroom"},
                }
            ]
        )
        client._request = AsyncMock(side_effect=Exception("REST bulk unavailable"))

        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            msg_type = message.get("type")
            if msg_type == "config/entity_registry/list":
                # Registry fetch FAILS → fallback engages.
                raise RuntimeError("simulated registry outage")
            return {"success": False}

        client.send_websocket_message = AsyncMock(side_effect=_ws_handler)
        # Per-id fetch SUCCEEDS — fallback recovers cleanly.
        client.get_scene_config = AsyncMock(
            return_value={
                "config": {
                    "id": "bedroom",
                    "name": "Bedroom",
                    "entities": {"light.bed": {"state": "on"}},
                }
            }
        )

        with _patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools_obj = SmartSearchTools(client=client)
            result = await tools_obj.deep_search(
                query="bedroom",
                search_types=["scene"],
                limit=10,
            )

        assert result["success"] is True
        # Critical: no partial flag because per-id fetches succeeded even
        # though the registry fetch failed.
        assert "partial" not in result, (
            f"clean per-id fallback must not surface partial=True; got {result}"
        )
        assert "partial_reason" not in result


class TestSmartSearchSceneIdIsStorageKey:
    """R6 blocker 17 — ``scene_id`` field in deep_search results must be the
    storage key (matching ``ha_config_get_scene``'s contract), not the
    entity_id slug derived at fetch time. A scene whose entity_id slug
    diverges from its storage key (renamed via UI) lets the bug surface.
    """

    async def test_deep_search_scene_id_uses_storage_key(self) -> None:
        from typing import Any
        from unittest.mock import MagicMock as _MagicMock
        from unittest.mock import patch as _patch

        from ha_mcp.tools.smart_search import SmartSearchTools

        # Storage key: "night_light_led_desk_strip"
        # Entity_id slug: "led_desk_strip_night_light" (HA derives from name)
        client = _MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "scene.led_desk_strip_night_light",
                    "state": "scening",
                    "attributes": {
                        "friendly_name": "LED Desk Strip Night Light",
                    },
                }
            ]
        )
        client._request = AsyncMock(side_effect=Exception("REST bulk unavailable"))

        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            msg_type = message.get("type")
            if msg_type in ("config/scene/config/list", "scene/config/list"):
                return {
                    "success": True,
                    "result": [
                        {
                            "id": "night_light_led_desk_strip",  # storage key
                            "name": "LED Desk Strip Night Light",
                            "entities": {
                                "light.led_desk_strip": {"state": "on"},
                            },
                        }
                    ],
                }
            if msg_type == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "scene.led_desk_strip_night_light",
                            "unique_id": "night_light_led_desk_strip",
                            "platform": "homeassistant",
                        }
                    ],
                }
            return {"success": False}

        client.send_websocket_message = AsyncMock(side_effect=_ws_handler)

        with _patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools_obj = SmartSearchTools(client=client)
            result = await tools_obj.deep_search(
                query="led desk strip",
                search_types=["scene"],
                limit=10,
                include_config=True,
            )

        scenes = result.get("scenes", [])
        assert len(scenes) == 1, f"expected 1 scene, got: {scenes}"
        match = scenes[0]
        # entity_id stays as the slug HA exposes.
        assert match["entity_id"] == "scene.led_desk_strip_night_light"
        # scene_id MUST be the storage key (matches ha_config_get_scene
        # contract — calling get_scene with scene_id from this result must
        # land in the same scene without a slug→storage-key remap surprise).
        assert match["scene_id"] == "night_light_led_desk_strip", (
            "scene_id field must carry the storage key, not the entity-id "
            "slug — otherwise deep_search → get_scene round-trips on a "
            "renamed scene rely on the resolver's slug-fallback rather than "
            "the explicit storage key the caller would otherwise pass back"
        )


class TestResolverRetriedFlag:
    """R6 blocker 19 — the exhausted-retry DEBUG log must only fire when the
    retry actually happened (both list calls returned without a match), not
    on every no-match exit. The legitimate fresh-create path that resolves
    on first try should not emit the "retry exhausted" message.
    """

    async def test_no_log_on_first_try_match(
        self,
        tools,
        mock_client,
        caplog,
    ) -> None:
        import logging

        # Registry returns the matching entry on the FIRST call — no retry
        # needed, no exhaustion log.
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "entity_id": "scene.movie_night",
                        "unique_id": "movie_night",
                        "platform": "homeassistant",
                    }
                ],
            }
        )

        caplog.set_level(logging.DEBUG, logger="ha_mcp.tools.tools_config_scenes")

        result = await tools._resolve_scene_entity_id("movie_night")
        assert result == "scene.movie_night"

        # No "exhausted" log on the success-on-first-try path.
        exhaust_logs = [
            r for r in caplog.records if "registry retry exhausted" in r.message.lower()
        ]
        assert not exhaust_logs, (
            "exhausted-retry log must not fire on first-try-success path; "
            f"got {[r.message for r in exhaust_logs]}"
        )

    async def test_log_only_after_retry_no_match(
        self,
        tools,
        mock_client,
        caplog,
    ) -> None:
        import logging

        # Registry returns NO match across both attempts — retry happens,
        # log fires.
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

        caplog.set_level(logging.DEBUG, logger="ha_mcp.tools.tools_config_scenes")

        result = await tools._resolve_scene_entity_id("missing_scene")
        assert result == "scene.missing_scene"

        exhaust_logs = [
            r for r in caplog.records if "registry retry exhausted" in r.message.lower()
        ]
        assert exhaust_logs, (
            f"exhausted-retry log must fire after the retry exits without "
            f"a match; got {[r.message for r in caplog.records]}"
        )


class TestCategoryValidationGate:
    """R8 follow-up — gate ``_validate_category_id`` on
    ``effective_category`` truthy, covering BOTH sources (user param and
    ``_validate_scene_config``-promoted top-level ``config["category"]``).

    The R7 fix gated on ``category is not None`` to skip the WS
    round-trip when no category was supplied, but that left the
    dict-promoted path uncovered: a phantom category in
    ``config["category"]`` would skip validation and reach
    ``apply_entity_category``, which attaches the phantom ID to the
    entity registry without checking it exists. R8 widens the gate so
    any non-None ``effective_category`` validates, regardless of source.
    """

    async def test_no_category_at_all_skips_validation(
        self,
        tools,
        mock_client,
    ):
        """Neither user param nor config dict supplies a category — the
        validator must NOT fire. No WS round-trip for category_registry/list."""
        ws_calls: list[dict] = []

        async def _ws_handler(message):
            ws_calls.append(message)
            return {"success": True, "result": []}

        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler)

        await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={"name": "X", "entities": {"light.x": {"state": "on"}}},
            category=None,
            wait=False,
        )

        category_list_calls = [
            c
            for c in ws_calls
            if isinstance(c, dict) and c.get("type") == "config/category_registry/list"
        ]
        assert not category_list_calls, (
            "_validate_category_id must NOT fire when no category is supplied "
            f"from either source. Got payloads: {category_list_calls}"
        )

    async def test_user_passed_category_validates(
        self,
        tools,
        mock_client,
    ):
        """User passes a non-None ``category`` — validator must fire."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"category_id": "lighting"}],
            }
        )

        await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={"name": "X", "entities": {"light.x": {"state": "on"}}},
            category="lighting",
            wait=False,
        )

        category_list_calls = [
            call.args[0]
            for call in mock_client.send_websocket_message.call_args_list
            if isinstance(call.args[0], dict)
            and call.args[0].get("type") == "config/category_registry/list"
        ]
        assert category_list_calls, (
            "_validate_category_id must fire when the user passes a category"
        )

    async def test_dict_promoted_category_validates(
        self,
        tools,
        mock_client,
    ):
        """User passes ``category=None`` but config has top-level
        ``config["category"]`` — ``_validate_scene_config`` promotes it
        to ``effective_category`` and the validator MUST fire so phantom
        IDs are caught before they reach ``apply_entity_category``."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"category_id": "lighting"}],
            }
        )

        await tools.ha_config_set_scene(
            scene_id="test_scene",
            config={
                "name": "X",
                "category": "lighting",  # top-level → promoted
                "entities": {"light.x": {"state": "on"}},
            },
            category=None,  # user didn't pass — promotion fires
            wait=False,
        )

        category_list_calls = [
            call.args[0]
            for call in mock_client.send_websocket_message.call_args_list
            if isinstance(call.args[0], dict)
            and call.args[0].get("type") == "config/category_registry/list"
        ]
        assert category_list_calls, (
            "_validate_category_id must fire when the config dict supplies "
            "a top-level category — phantom IDs in this path were attached "
            "to the entity registry without validation in R7."
        )

    async def test_dict_promoted_phantom_rejected_pre_upsert(
        self,
        tools,
        mock_client,
    ):
        """B25 regression — phantom category in top-level ``config["category"]``
        must be rejected pre-upsert. Live reproducer: caller sets
        ``config={"category": "phantom_id", ...}`` with no ``category=`` param;
        the R7 gate skipped validation, the upsert committed, and the phantom
        ID was attached to the entity registry. R8 widens the gate so this
        path validates and rejects before any state mutation."""
        # Registry returns no matching category — phantom rejected.
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_scene(
                scene_id="test_scene",
                config={
                    "name": "X",
                    "category": "phantom_via_config_dict",
                    "entities": {"light.x": {"state": "on"}},
                },
                category=None,
                wait=False,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "phantom_via_config_dict" in error_data["error"]["message"]
        # The whole point: no upsert reached HA.
        mock_client.upsert_scene_config.assert_not_called()


class TestPythonTransformIdNoneRebind:
    """R7 blocker 24 — a transform that sets ``config['id'] = None``
    explicitly is the in-place equivalent of ``del config['id']`` (HA
    treats ``id`` as optional). The R6 strict check rejected it with
    a misleading "rename detected" error because ``None != resolved_id``.
    R7 normalises both as missing-key.
    """

    async def test_transform_setting_id_to_none_passes_through(
        self,
        tools,
        mock_client,
    ):
        from ha_mcp.utils.config_hash import compute_config_hash

        seed = {
            "id": "test_scene",
            "name": "Test Scene",
            "entities": {"light.kitchen": {"state": "on"}},
        }
        mock_client.get_scene_config = AsyncMock(return_value=seed)
        seed_hash = compute_config_hash(seed)

        result = await tools.ha_config_set_scene(
            scene_id="test_scene",
            python_transform="config['id'] = None",
            config_hash=seed_hash,
            wait=False,
        )

        assert result["success"] is True
        assert result["action"] == "python_transform"
        # The upsert was called — id=None is legitimate.
        mock_client.upsert_scene_config.assert_called_once()
        # The transform's None passed through; the upsert sees id=None.
        sent_config = mock_client.upsert_scene_config.call_args.args[0]
        assert sent_config.get("id") is None


class TestSmartSearchSceneIdFallbackPaths:
    """R7 blockers 17/21 — three-tier resolution of ``scene_id`` in
    ``ha_deep_search`` results: (1) ``scene_config["id"]``, (2) registry-
    derived map, (3) entity-id slug + WARNING.

    The R6 fix only covered tier 1 (alias-hit path); the empty-dict
    fallback silently returned the slug. R7 adds the registry-derived
    map (tier 2) and a logger.warning (tier 3 visibility).
    """

    async def test_scene_with_no_bulk_config_uses_registry_map(self) -> None:
        """When the bulk fetch omits a scene's ``id`` field but the
        registry walk supplies the slug→storage map, the result-builder
        uses the map's storage key (tier 2)."""
        from typing import Any
        from unittest.mock import MagicMock as _MagicMock
        from unittest.mock import patch as _patch

        from ha_mcp.tools.smart_search import SmartSearchTools

        client = _MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "scene.bat_distinct_friendly_name",
                    "state": "scening",
                    "attributes": {
                        "friendly_name": "BAT Distinct Friendly Name",
                    },
                }
            ]
        )
        client._request = AsyncMock(side_effect=Exception("REST bulk unavailable"))

        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            msg_type = message.get("type")
            if msg_type in ("config/scene/config/list", "scene/config/list"):
                # Bulk omits the ``id`` field — simulates the regression case.
                return {
                    "success": True,
                    "result": [
                        {
                            # NO ``id`` field
                            "name": "BAT Distinct Friendly Name",
                            "entities": {"light.x": {"state": "on"}},
                        }
                    ],
                }
            if msg_type == "config/entity_registry/list":
                # Registry knows the storage key.
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "scene.bat_distinct_friendly_name",
                            "unique_id": "bat_storkey_alpha",
                            "platform": "homeassistant",
                        }
                    ],
                }
            return {"success": False}

        client.send_websocket_message = AsyncMock(side_effect=_ws_handler)

        with _patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools_obj = SmartSearchTools(client=client)
            result = await tools_obj.deep_search(
                query="distinct friendly",
                search_types=["scene"],
                limit=10,
                include_config=True,
            )

        scenes = result.get("scenes", [])
        assert scenes, f"expected 1 scene match, got: {scenes}"
        # Tier 2 map provides the storage key — NOT the entity-id slug.
        assert scenes[0]["scene_id"] == "bat_storkey_alpha", (
            "registry-derived slug→storage map must supply the storage key "
            "when bulk config omits ``id``; got "
            f"{scenes[0]['scene_id']!r}"
        )
        assert scenes[0]["entity_id"] == "scene.bat_distinct_friendly_name"

    async def test_scene_falls_back_to_slug_with_warning(
        self,
        caplog,
    ) -> None:
        """When neither the bulk config nor the registry walk produced a
        storage key for a scene, the result-builder falls back to the
        entity-id slug AND emits a WARNING — the silent-slug-mismatch
        path is now observable."""
        import logging
        from typing import Any
        from unittest.mock import MagicMock as _MagicMock
        from unittest.mock import patch as _patch

        from ha_mcp.tools.smart_search import SmartSearchTools

        client = _MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "scene.orphaned",
                    "state": "scening",
                    "attributes": {"friendly_name": "Orphaned Scene"},
                }
            ]
        )
        client._request = AsyncMock(side_effect=Exception("REST bulk unavailable"))

        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            # Both bulk and registry return empty — neither tier 1 nor 2.
            return {"success": True, "result": []}

        client.send_websocket_message = AsyncMock(side_effect=_ws_handler)
        client.get_scene_config = AsyncMock(return_value=None)

        caplog.set_level(logging.WARNING, logger="ha_mcp.tools.smart_search")

        with _patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools_obj = SmartSearchTools(client=client)
            result = await tools_obj.deep_search(
                query="orphaned",
                search_types=["scene"],
                limit=10,
            )

        scenes = result.get("scenes", [])
        assert scenes, (
            "tier-3 must yield a result for the matched scene, not drop it. "
            "A regression that silently omits orphaned entries would produce "
            f"empty scenes; got {scenes!r}"
        )
        # Tier 3 fallback — slug stays as scene_id.
        assert scenes[0]["scene_id"] == "orphaned"
        # WARNING log fired so the silent path becomes observable.
        warn_records = [
            r
            for r in caplog.records
            if "fell back to entity-id slug" in r.message
            and r.levelno == logging.WARNING
        ]
        assert warn_records, (
            "tier-3 slug-fallback must emit a WARNING; got "
            f"{[(r.levelname, r.message) for r in caplog.records]}"
        )
