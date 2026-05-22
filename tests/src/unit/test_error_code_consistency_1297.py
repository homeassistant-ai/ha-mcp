"""Regression tests for issue #1297 D1 error-shape consistency work.

Label / category / device / zone not-found maps to ``RESOURCE_NOT_FOUND``,
not ``ENTITY_NOT_FOUND``. These are registry metadata (labels, categories)
or their own non-entity registries (devices, zones), all looked up by
registry-internal id rather than entity_id. An agent branching on
``error.code == "ENTITY_NOT_FOUND"`` retries via ``ha_search_entities()``,
which doesn't list any of them — wrong-tool spiral. (Callers here supply
explicit suggestions, so the ``ENTITY_NOT_FOUND`` *default* suggestion
table in ``errors.py:129-133`` doesn't surface; the agent-side
classification by ``error.code`` is the leak path the fix closes.)

The complementary D2 work (dashboards / dashboard-resource not-found
suggestions) landed via #1386 and is covered there by
``TestDeleteDashboardNotFoundShape`` in ``test_tools_config_dashboards.py``.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError


@pytest.fixture
def mock_ws_client():
    """Mock client with an AsyncMock send_websocket_message ready for per-test programming."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return client


def _all_suggestions(error_payload: dict[str, Any]) -> list[str]:
    """Collect every suggestion regardless of which field holds it.

    ``create_error_response`` writes the first suggestion into the singular
    ``suggestion`` key and only emits the plural ``suggestions`` list when
    there are two or more — so a single-suggestion caller produces only
    ``suggestion``. Tests need to look at both.
    """
    singular = error_payload.get("suggestion")
    plural = error_payload.get("suggestions") or []
    return ([singular] if singular else []) + list(plural)


# ---------------------------------------------------------------------------
# D1 — Label / Category get-by-id → RESOURCE_NOT_FOUND (was ENTITY_NOT_FOUND)
# ---------------------------------------------------------------------------


class TestLabelGetMissingReturnsResourceNotFound:
    """Regression: ha_config_get_label(missing) must surface
    ``RESOURCE_NOT_FOUND``. Pre-#1297 it surfaced ``ENTITY_NOT_FOUND``,
    routing agents to the entity-search path for non-entity metadata.
    """

    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_labels import LabelTools

        return LabelTools(mock_ws_client)

    async def test_missing_label_id_surfaces_resource_not_found(
        self, tools, mock_ws_client
    ):
        mock_ws_client.send_websocket_message.return_value = {
            "success": True,
            "result": [{"label_id": "existing", "name": "Existing"}],
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_label(label_id="missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Labels are registry metadata, not entities — must classify as "
            "RESOURCE_NOT_FOUND so agents route to ha_config_get_label() "
            "instead of ha_search_entities()."
        )
        # The list-tool recovery suggestion (already pre-#1297) must survive.
        assert any(
            "ha_config_get_label" in s for s in _all_suggestions(error_data["error"])
        )
        # The available_label_ids surface (pre-#1297) must survive too.
        assert "available_label_ids" in error_data


class TestCategoryGetMissingReturnsResourceNotFound:
    """Regression: ha_config_get_category(scope, missing) must surface
    ``RESOURCE_NOT_FOUND``. Same reasoning as labels.
    """

    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_categories import CategoryTools

        return CategoryTools(mock_ws_client)

    async def test_missing_category_id_surfaces_resource_not_found(
        self, tools, mock_ws_client
    ):
        mock_ws_client.send_websocket_message.return_value = {
            "success": True,
            "result": [{"category_id": "existing", "name": "Existing"}],
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_category(
                scope="automation", category_id="missing"
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Categories are registry metadata, not entities — must classify "
            "as RESOURCE_NOT_FOUND."
        )
        assert any(
            "ha_config_get_category" in s for s in _all_suggestions(error_data["error"])
        )
        assert "available_category_ids" in error_data
        assert error_data["scope"] == "automation"


class TestZoneGetMissingReturnsResourceNotFound:
    """Regression: ha_get_zone(zone_id=missing) must surface
    ``RESOURCE_NOT_FOUND``. Same pattern as labels/categories: registry-
    internal ``zone_id`` lookup (not entity_id), with an explicit
    ``ha_get_zone()`` recovery suggestion that the auto-injected entity-
    search hint would have overridden.
    """

    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_zones import ZoneTools

        return ZoneTools(mock_ws_client)

    async def test_missing_zone_id_surfaces_resource_not_found(
        self, tools, mock_ws_client
    ):
        mock_ws_client.send_websocket_message.return_value = {
            "success": True,
            "result": [{"id": "existing_zone", "name": "Existing"}],
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_get_zone(zone_id="missing_zone")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Zones are addressed by registry-internal zone_id here, not by "
            "entity_id — RESOURCE_NOT_FOUND is the correct category."
        )
        assert any("ha_get_zone" in s for s in _all_suggestions(error_data["error"]))
        assert "available_zone_ids" in error_data


def _register_registry_tools_and_capture(mock_client):
    """Closure-pattern capture for tools_registry (same shape as tools_config_dashboards)."""
    from ha_mcp.tools.tools_registry import register_registry_tools

    mock_mcp = MagicMock()
    captured: dict[str, Any] = {}

    def fake_tool(**kwargs):
        def decorator(fn):
            captured[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp.tool = fake_tool
    register_registry_tools(mock_mcp, mock_client)
    return captured


class TestDeviceLookupMissingReturnsResourceNotFound:
    """Sibling-bug fix discovered during the #1297 cross-family audit:
    ``ha_get_device(device_id=missing)`` and ``ha_remove_device(device_id=missing)``
    previously raised ``ENTITY_NOT_FOUND``. Devices are NOT entities — they live
    in the device registry, addressed by device_id (UUID), with their own
    suggestion (``ha_get_device()``). Same mis-classification class as the
    labels/categories sites above.
    """

    async def test_get_device_missing_id_surfaces_resource_not_found(
        self, mock_ws_client
    ):
        # Two list-registry calls: device_registry/list + entity_registry/list.
        # First call returns a small device set so the test can also pin the
        # ``available_device_ids`` parity with the sibling labels/categories
        # sites (KP13 review #2 — first-10 truncation must surface as context).
        mock_ws_client.send_websocket_message = AsyncMock(
            side_effect=[
                {
                    "success": True,
                    "result": [{"id": "dev-existing-1"}, {"id": "dev-existing-2"}],
                },
                {"success": True, "result": []},  # entity_registry/list
            ]
        )
        captured = _register_registry_tools_and_capture(mock_ws_client)
        ha_get_device = captured["ha_get_device"]

        with pytest.raises(ToolError) as exc_info:
            await ha_get_device(device_id="missing-device-uuid")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Devices are not entities — must classify as RESOURCE_NOT_FOUND."
        )
        assert any("ha_get_device" in s for s in _all_suggestions(error_data["error"]))
        assert error_data.get("available_device_ids") == [
            "dev-existing-1",
            "dev-existing-2",
        ]

    async def test_remove_device_missing_id_surfaces_resource_not_found(
        self, mock_ws_client
    ):
        mock_ws_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"id": "dev-existing-1"}, {"id": "dev-existing-2"}],
            }
        )
        captured = _register_registry_tools_and_capture(mock_ws_client)
        ha_remove_device = captured["ha_remove_device"]

        with pytest.raises(ToolError) as exc_info:
            await ha_remove_device(device_id="missing-device-uuid")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            "Devices are not entities — must classify as RESOURCE_NOT_FOUND."
        )
        assert any("ha_get_device" in s for s in _all_suggestions(error_data["error"]))
        assert error_data.get("available_device_ids") == [
            "dev-existing-1",
            "dev-existing-2",
        ]


# ---------------------------------------------------------------------------
# Mutation paths — KP13 #1397 review item 4: set / remove on missing registry
# id should classify as RESOURCE_NOT_FOUND, not the generic SERVICE_CALL_FAILED.
# Per-site WS "not found" substring match (HA Core surfaces it consistently in
# the WS-response error string).
# ---------------------------------------------------------------------------


class TestLabelMutationRoutesNotFoundToResourceNotFound:
    """Label set-update / remove with a non-existent ``label_id`` must surface
    ``RESOURCE_NOT_FOUND``, mirroring the GET-path classification.
    """

    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_labels import LabelTools

        return LabelTools(mock_ws_client)

    async def test_set_update_with_missing_label_id(self, tools, mock_ws_client):
        mock_ws_client.send_websocket_message.return_value = {
            "success": False,
            "error": "Label not found",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_label(name="X", label_id="missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any(
            "ha_config_get_label" in s for s in _all_suggestions(error_data["error"])
        )

    async def test_remove_with_missing_label_id(self, tools, mock_ws_client):
        mock_ws_client.send_websocket_message.return_value = {
            "success": False,
            "error": "Label not found",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_label(label_id="missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any(
            "ha_config_get_label" in s for s in _all_suggestions(error_data["error"])
        )


class TestCategoryMutationRoutesNotFoundToResourceNotFound:
    """Category set-update / remove with a non-existent ``category_id`` must
    surface ``RESOURCE_NOT_FOUND``.
    """

    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_categories import CategoryTools

        return CategoryTools(mock_ws_client)

    async def test_set_update_with_missing_category_id(self, tools, mock_ws_client):
        mock_ws_client.send_websocket_message.return_value = {
            "success": False,
            "error": "Category not found",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_category(
                name="X", scope="automation", category_id="missing"
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any(
            "ha_config_get_category" in s for s in _all_suggestions(error_data["error"])
        )

    async def test_remove_with_missing_category_id(self, tools, mock_ws_client):
        mock_ws_client.send_websocket_message.return_value = {
            "success": False,
            "error": "Category not found",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_category(
                scope="automation", category_id="missing"
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any(
            "ha_config_get_category" in s for s in _all_suggestions(error_data["error"])
        )

    async def test_remove_with_doesnt_exist_phrasing(self, tools, mock_ws_client):
        """HA Core's category-registry remove path surfaces the not-found case
        with the phrasing ``"Category ID doesn't exist"`` rather than the more
        common ``"not found"`` (observed live in PR #1397 CI on commit 70b9edf).
        The substring check must accept both phrasings.
        """
        mock_ws_client.send_websocket_message.return_value = {
            "success": False,
            "error": "Category ID doesn't exist",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_category(
                scope="automation", category_id="missing"
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"


class TestZoneMutationRoutesNotFoundToResourceNotFound:
    """Zone set-update / remove with a non-existent ``zone_id`` must surface
    ``RESOURCE_NOT_FOUND``.
    """

    @pytest.fixture
    def tools(self, mock_ws_client):
        from ha_mcp.tools.tools_zones import ZoneTools

        return ZoneTools(mock_ws_client)

    async def test_set_update_with_missing_zone_id(self, tools, mock_ws_client):
        mock_ws_client.send_websocket_message.return_value = {
            "success": False,
            "error": "Zone not found",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_zone(name="X", zone_id="missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any("ha_get_zone" in s for s in _all_suggestions(error_data["error"]))

    async def test_remove_with_missing_zone_id(self, tools, mock_ws_client):
        mock_ws_client.send_websocket_message.return_value = {
            "success": False,
            "error": "Zone not found",
        }

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_remove_zone(zone_id="missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any("ha_get_zone" in s for s in _all_suggestions(error_data["error"]))


# ---------------------------------------------------------------------------
# D4 — available_*_ids parity for dashboards / automations / scripts
# (KP13 #1397 second-pass review): the same audit family extended to the
# three remaining sites whose RESOURCE_NOT_FOUND raise was missing the
# ``available_*_ids`` payload that labels / categories / zones / devices
# all surface. Pinning matches the sibling depth — error code + list-tool
# suggestion + ``available_*_ids`` payload truncated to first-10.
# ---------------------------------------------------------------------------


class TestDeleteDashboardNotFoundSurfacesAvailableIds:
    """Regression: ``ha_config_delete_dashboard`` with an unresolvable
    url_path must populate ``available_dashboard_ids`` (first-10) in the
    error context, mirroring the sibling registries.
    ``_resolve_dashboard`` already returns the dashboards list alongside
    the match — the fix re-uses it instead of discarding via ``_``.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def delete_tool(self, mock_client):
        from ha_mcp.tools.tools_config_dashboards import (
            register_config_dashboard_tools,
        )

        mcp = MagicMock()
        registered: dict[str, Any] = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                registered[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        register_config_dashboard_tools(mcp, mock_client)
        return registered["ha_config_delete_dashboard"]

    async def test_missing_dashboard_surfaces_available_dashboard_ids(
        self, delete_tool, mock_client
    ):
        # Registry list with three entries; the requested url_path is absent.
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [
                {"id": "dash-a", "url_path": "alpha-dash"},
                {"id": "dash-b", "url_path": "beta-dash"},
                {"id": "dash-c", "url_path": "gamma-dash"},
            ],
        }

        with pytest.raises(ToolError) as exc_info:
            await delete_tool(url_path="ghost-dash")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data["action"] == "delete"
        assert error_data["url_path"] == "ghost-dash"
        # First-10 url_paths surface; here the registry only had 3 so all 3
        # appear. Mirrors the sibling labels/categories/zones first-10 cap.
        assert error_data.get("available_dashboard_ids") == [
            "alpha-dash",
            "beta-dash",
            "gamma-dash",
        ]
        assert any(
            "ha_config_get_dashboard" in s
            for s in _all_suggestions(error_data["error"])
        )

    async def test_resolver_unexpected_shape_yields_empty_available_ids(
        self, delete_tool, mock_client
    ):
        """When ``_resolve_dashboard`` returns ``(None, None)`` because the
        WS response shape was unexpected, the structured raise still fires
        with ``available_dashboard_ids: []`` — the ``(dashboards or [])[:10]``
        guard at the raise site prevents a NoneType subscript while still
        surfacing the canonical error shape.
        """
        # String response triggers the "unexpected shape" branch in
        # _fetch_dashboards_list, which causes _resolve_dashboard to return
        # (None, None).
        mock_client.send_websocket_message.return_value = "garbage"

        with pytest.raises(ToolError) as exc_info:
            await delete_tool(url_path="ghost-dash")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("available_dashboard_ids") == []


class TestGetAutomationMissingSurfacesAvailableIds:
    """Regression: ``ha_config_get_automation`` with a 404 from the REST
    client must surface ``RESOURCE_NOT_FOUND`` + ``available_automation_ids``
    (first-10 from the entity registry, filtered by ``automation.`` prefix).
    Pre-#1397-D4 the 404 surfaced as ``RESOURCE_NOT_FOUND`` via the generic
    ``_classify_api_status`` route (``helpers.py:199-205``), but without
    ``available_automation_ids`` or the list-tool suggestion that sibling
    sites populate. The fix adds the payload + suggestion at the central
    not-found raise inside ``_get_automation_config_internal``.
    """

    @pytest.fixture
    def mock_client(self):
        from ha_mcp.client.rest_client import HomeAssistantAPIError

        client = MagicMock()
        client.get_automation_config = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "Automation not found: automation.missing",
                status_code=404,
            )
        )
        # Entity registry list returns both automation and non-automation
        # entries; the helper must filter by domain prefix.
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"entity_id": "automation.morning_routine"},
                    {"entity_id": "automation.evening_routine"},
                    {"entity_id": "script.something"},  # filtered out
                    {"entity_id": "light.bedroom"},  # filtered out
                ],
            }
        )
        client.get_services = AsyncMock(return_value={})
        client.get_states = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_automations import AutomationConfigTools

        return AutomationConfigTools(mock_client)

    async def test_missing_automation_surfaces_available_automation_ids(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_automation(identifier="automation.missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("automation_id") == "automation.missing"
        # First-10 entries filtered to automation. prefix.
        assert error_data.get("available_automation_ids") == [
            "automation.morning_routine",
            "automation.evening_routine",
        ]
        assert any(
            "ha_search_entities(domain_filter='automation')" in s
            for s in _all_suggestions(error_data["error"])
        )

    async def test_registry_list_failure_yields_empty_available_ids(
        self, tools, mock_client
    ):
        """Best-effort: when ``entity_registry/list`` fails, the structured
        not-found raise still fires with ``available_automation_ids: []``
        rather than masking the 404 behind the registry error.
        """
        mock_client.send_websocket_message = AsyncMock(
            side_effect=RuntimeError("ws transport closed")
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_automation(identifier="automation.missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("available_automation_ids") == []


class TestGetScriptMissingSurfacesAvailableIds:
    """Regression: ``ha_config_get_script`` with a 404 from the REST client
    must surface ``RESOURCE_NOT_FOUND`` + ``available_script_ids`` (first-10
    bare IDs from the entity registry, filtered by ``script.`` prefix and
    stripped to the bare form ``ha_config_get_script(script_id=...)`` takes).
    Behavioral parity with ``ha_config_get_automation`` — both tools accept
    either the bare storage key (``foo``) or the entity_id form
    (``script.foo`` / ``automation.foo``). The mechanism differs: automations
    resolve via state lookup (``_resolve_automation_entity_id``); scripts
    strip a leading ``script.`` prefix at the function head before the REST
    call.
    """

    @pytest.fixture
    def mock_client(self):
        from ha_mcp.client.rest_client import HomeAssistantAPIError

        client = MagicMock()
        client.get_script_config = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "Script not found: missing_script",
                status_code=404,
            )
        )
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"entity_id": "script.morning_routine"},
                    {"entity_id": "script.evening_routine"},
                    {"entity_id": "automation.something"},  # filtered out
                ],
            }
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

        return ConfigScriptTools(mock_client)

    async def test_missing_script_surfaces_available_script_ids(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_script(script_id="missing_script")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("script_id") == "missing_script"
        # Bare IDs (stripped of the ``script.`` entity_id prefix) — what
        # ``ha_config_get_script(script_id=...)`` takes for the retry.
        assert error_data.get("available_script_ids") == [
            "morning_routine",
            "evening_routine",
        ]
        assert any(
            "ha_search_entities(domain_filter='script')" in s
            for s in _all_suggestions(error_data["error"])
        )

    async def test_registry_list_failure_yields_empty_available_ids(
        self, tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=RuntimeError("ws transport closed")
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_script(script_id="missing_script")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("available_script_ids") == []


class TestGetScriptAcceptsEntityIdForm:
    """Regression: ``ha_config_get_script`` strips a leading ``script.``
    prefix so the entity_id form (``script.foo``) and the bare storage key
    (``foo``) both route to the same REST call. Closes the wrong-tool spiral
    where ``_raise_script_not_found`` suggests
    ``ha_search_entities(domain_filter='script')`` which returns entity_ids
    — feeding that output back into a bare-only GET would re-fail at the
    same site that #1297 closes (KP13 #1397 fourth-pass).
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_script_config = AsyncMock(
            return_value={
                "script_id": "morning_routine",
                "config": {"sequence": [], "mode": "single"},
            }
        )
        # fetch_entity_category goes through send_websocket_message
        # and expects ``result.result.categories`` — return an empty
        # categories map so category-injection is a no-op.
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"categories": {}}}
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

        return ConfigScriptTools(mock_client)

    async def test_entity_id_form_routes_to_bare_storage_key(self, tools, mock_client):
        result = await tools.ha_config_get_script(script_id="script.morning_routine")

        mock_client.get_script_config.assert_awaited_once_with("morning_routine")
        assert result["success"] is True
        assert result["script_id"] == "morning_routine"

    async def test_bare_form_unchanged(self, tools, mock_client):
        result = await tools.ha_config_get_script(script_id="morning_routine")

        mock_client.get_script_config.assert_awaited_once_with("morning_routine")
        assert result["success"] is True
        assert result["script_id"] == "morning_routine"

    async def test_only_leading_prefix_is_stripped(self, tools, mock_client):
        # A bare key that happens to contain ``script.`` as a substring
        # (not as a prefix) must pass through untouched.
        mock_client.get_script_config = AsyncMock(
            return_value={
                "script_id": "my_script.backup",
                "config": {"sequence": []},
            }
        )
        result = await tools.ha_config_get_script(script_id="my_script.backup")

        mock_client.get_script_config.assert_awaited_once_with("my_script.backup")
        assert result["script_id"] == "my_script.backup"

    async def test_nested_prefix_one_layer_strip(self, tools, mock_client):
        # ``removeprefix`` strips exactly one leading occurrence, so
        # ``script.script.foo`` becomes ``script.foo`` (not ``foo``). The
        # second ``script.`` is preserved as part of the bare key.
        mock_client.get_script_config = AsyncMock(
            return_value={
                "script_id": "script.foo",
                "config": {"sequence": []},
            }
        )
        result = await tools.ha_config_get_script(script_id="script.script.foo")

        mock_client.get_script_config.assert_awaited_once_with("script.foo")
        assert result["script_id"] == "script.foo"


class TestScriptStripBeforeValidate:
    """Regression: ``"script."`` (entity_id form with empty bare key) must
    surface ``VALIDATION_INVALID_PARAMETER`` from
    ``validate_identifier_not_empty``, not slip through validate
    (non-empty pre-strip) and 404 at the downstream REST call. Pinned for
    all three tools that perform the strip (KP13 #1397 fifth-pass).
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_services = AsyncMock(return_value={})
        client.get_states = AsyncMock(return_value=[])
        client.get_script_config = AsyncMock(
            return_value={"script_id": "", "config": {}}
        )
        client.upsert_script_config = AsyncMock(return_value={})
        client.delete_script_config = AsyncMock(return_value={})
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

        return ConfigScriptTools(mock_client)

    async def test_get_script_empty_after_strip_is_invalid_parameter(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_get_script(script_id="script.")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        mock_client.get_script_config.assert_not_called()

    async def test_set_script_empty_after_strip_is_invalid_parameter(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="script.",
                config={"sequence": [{"delay": {"seconds": 1}}]},
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        mock_client.upsert_script_config.assert_not_called()

    async def test_remove_script_empty_after_strip_is_invalid_parameter(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_script(script_id="script.")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        mock_client.delete_script_config.assert_not_called()


class TestSetRemoveScriptAcceptEntityIdForm:
    """Regression: ``ha_config_set_script`` and ``ha_config_remove_script``
    strip a leading ``script.`` prefix at the function head, mirroring
    ``ha_config_get_script`` (KP13 #1397 fifth-pass). Closes the wrong-tool
    spiral where ``ha_search_entities(domain_filter='script')`` returns
    entity_ids that would otherwise produce phantom ``script.script.foo``
    storage keys on upsert / ``script.script.foo`` watcher targets on remove.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_services = AsyncMock(return_value={})
        client.get_states = AsyncMock(return_value=[])
        client.upsert_script_config = AsyncMock(
            return_value={"script_id": "morning_routine"}
        )
        client.delete_script_config = AsyncMock(return_value={"success": True})
        client.get_script_config = AsyncMock(
            return_value={
                "script_id": "morning_routine",
                "config": {"sequence": [], "mode": "single"},
            }
        )
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

        return ConfigScriptTools(mock_client)

    async def test_set_script_entity_id_form_routes_to_bare_key(
        self, tools, mock_client
    ):
        await tools.ha_config_set_script(
            script_id="script.morning_routine",
            config={"sequence": [{"delay": {"seconds": 1}}]},
            wait=False,
        )

        # Upsert is called with the bare storage key — not the entity_id
        # form — so the registry doesn't get a phantom ``script.script.*``.
        mock_client.upsert_script_config.assert_awaited_once()
        _, called_script_id = mock_client.upsert_script_config.call_args[0]
        assert called_script_id == "morning_routine"

    async def test_remove_script_entity_id_form_routes_to_bare_key(
        self, tools, mock_client
    ):
        await tools.ha_config_remove_script(
            script_id="script.morning_routine",
            wait=False,
        )

        mock_client.delete_script_config.assert_awaited_once_with("morning_routine")

    async def test_remove_script_wait_watcher_target_is_post_strip(
        self, tools, mock_client
    ):
        # The strip's load-bearing job on the remove path is preventing the
        # ``script.script.foo`` watcher phantom — ``entity_id = f"script.
        # {script_id}"`` at tools_config_scripts.py:848 builds the watcher
        # target post-strip. A future refactor that drops the strip would
        # regress silently if only the ``wait=False`` test exists.
        from unittest.mock import patch

        with patch(
            "ha_mcp.tools.tools_config_scripts.wait_for_entity_removed",
            new=AsyncMock(return_value=True),
        ) as mock_watcher:
            await tools.ha_config_remove_script(
                script_id="script.morning_routine",
                wait=True,
            )

        mock_client.delete_script_config.assert_awaited_once_with("morning_routine")
        mock_watcher.assert_awaited_once()
        _, watcher_entity_id = mock_watcher.call_args[0]
        assert watcher_entity_id == "script.morning_routine"


# ---------------------------------------------------------------------------
# Mutation-path parity for automations / scripts (KP13 #1397 third-pass): the
# 404 → RESOURCE_NOT_FOUND mapping in ``_classify_api_status`` already routed
# correctly, but without the ``available_*_ids`` payload + list-tool
# suggestion that sibling sites surface. The fix wires
# ``_raise_*_not_found`` into the set/remove except chains so the mutation
# path matches the GET path one-for-one.
# ---------------------------------------------------------------------------


def _api_404(message: str) -> Any:
    """Shorthand for an HA REST 404 the helpers should map to RESOURCE_NOT_FOUND."""
    from ha_mcp.client.rest_client import HomeAssistantAPIError

    return HomeAssistantAPIError(message, status_code=404)


class TestSetAutomationMissingSurfacesAvailableIds:
    """Update path: ``ha_config_set_automation(identifier=..., config=...)``
    with a 404 from the REST upsert (identifier resolves to a no-longer-
    existing automation) must surface ``RESOURCE_NOT_FOUND`` with
    ``available_automation_ids``. The create branch (identifier=None) is
    guarded by the ``identifier and`` check so it falls through to the
    generic exception_to_structured_error route untouched.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        # Resolve path (best-effort) — keep it silent for this test.
        client.get_states = AsyncMock(return_value=[])
        # Reference validator (validate_config_references) hits get_services
        # + get_states — both empty is fine.
        client.get_services = AsyncMock(return_value={})
        # The actual upsert raises 404 — the audit-family failure mode.
        client.upsert_automation_config = AsyncMock(
            side_effect=_api_404("Automation not found: automation.missing")
        )
        # Entity registry list for available_automation_ids payload.
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"entity_id": "automation.morning_routine"},
                    {"entity_id": "automation.evening_routine"},
                ],
            }
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_automations import AutomationConfigTools

        return AutomationConfigTools(mock_client)

    async def test_update_with_missing_identifier_surfaces_available_ids(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_automation(
                identifier="automation.missing",
                config={
                    "alias": "X",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on"}],
                },
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("automation_id") == "automation.missing"
        assert error_data.get("available_automation_ids") == [
            "automation.morning_routine",
            "automation.evening_routine",
        ]
        assert any(
            "ha_search_entities(domain_filter='automation')" in s
            for s in _all_suggestions(error_data["error"])
        )


class TestRemoveAutomationMissingSurfacesAvailableIds:
    """Delete path: ``ha_config_remove_automation(identifier=...)`` against a
    no-longer-existing automation must surface ``RESOURCE_NOT_FOUND`` with
    ``available_automation_ids``.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        client.delete_automation_config = AsyncMock(
            side_effect=_api_404("Automation not found: automation.missing")
        )
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"entity_id": "automation.morning_routine"},
                    {"entity_id": "automation.evening_routine"},
                ],
            }
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_automations import AutomationConfigTools

        return AutomationConfigTools(mock_client)

    async def test_remove_with_missing_identifier_surfaces_available_ids(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_automation(identifier="automation.missing")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("automation_id") == "automation.missing"
        assert error_data.get("available_automation_ids") == [
            "automation.morning_routine",
            "automation.evening_routine",
        ]


class TestSetScriptMissingSurfacesAvailableIds:
    """Update path: ``ha_config_set_script(script_id=..., config=...)`` with
    a 404 from the REST upsert must surface ``RESOURCE_NOT_FOUND`` with
    ``available_script_ids`` (bare form, no ``script.`` prefix).
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_services = AsyncMock(return_value={})
        client.get_states = AsyncMock(return_value=[])
        client.upsert_script_config = AsyncMock(
            side_effect=_api_404("Script not found: missing_script")
        )
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"entity_id": "script.morning_routine"},
                    {"entity_id": "script.evening_routine"},
                ],
            }
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

        return ConfigScriptTools(mock_client)

    async def test_update_with_missing_script_id_surfaces_available_ids(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="missing_script",
                config={
                    "alias": "X",
                    "sequence": [{"delay": {"seconds": 1}}],
                },
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("script_id") == "missing_script"
        assert error_data.get("available_script_ids") == [
            "morning_routine",
            "evening_routine",
        ]
        assert any(
            "ha_search_entities(domain_filter='script')" in s
            for s in _all_suggestions(error_data["error"])
        )


class TestRemoveScriptMissingSurfacesAvailableIds:
    """Delete path: ``ha_config_remove_script(script_id=...)`` against a
    no-longer-existing script must surface ``RESOURCE_NOT_FOUND`` with
    ``available_script_ids`` (bare form).
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.delete_script_config = AsyncMock(
            side_effect=_api_404("Script not found: missing_script")
        )
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"entity_id": "script.morning_routine"},
                    {"entity_id": "script.evening_routine"},
                ],
            }
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        from ha_mcp.tools.tools_config_scripts import ConfigScriptTools

        return ConfigScriptTools(mock_client)

    async def test_remove_with_missing_script_id_surfaces_available_ids(
        self, tools, mock_client
    ):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_script(script_id="missing_script")

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error_data.get("script_id") == "missing_script"
        assert error_data.get("available_script_ids") == [
            "morning_routine",
            "evening_routine",
        ]
