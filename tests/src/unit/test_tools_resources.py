"""Unit tests for dashboard resource tools."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_resources import (
    MAX_CONTENT_SIZE,
    MAX_ENCODED_LENGTH,
    WORKER_BASE_URL,
    ResourceTools,
    _decode_inline_url,
    _detect_ha_config_yaml,
    _encode_content,
    _is_inline_url,
    register_resources_tools,
)


class TestHelperFunctions:
    """Test helper functions."""

    def test_encode_content(self):
        """Test content encoding."""
        content = "test content"
        encoded, content_size, encoded_size = _encode_content(content)

        assert content_size == len(content.encode("utf-8"))
        assert encoded_size == len(encoded)
        assert base64.urlsafe_b64decode(encoded).decode("utf-8") == content

    def test_is_inline_url_true(self):
        """Test inline URL detection."""
        url = f"{WORKER_BASE_URL}/abc123?type=module"
        assert _is_inline_url(url) is True

    def test_is_inline_url_false(self):
        """Test non-inline URL detection."""
        assert _is_inline_url("/local/card.js") is False
        assert _is_inline_url("https://cdn.example.com/card.js") is False

    def test_decode_inline_url(self):
        """Test decoding inline URL."""
        content = "export const x = 1;"
        encoded = base64.urlsafe_b64encode(content.encode()).decode()
        url = f"{WORKER_BASE_URL}/{encoded}?type=module"

        decoded = _decode_inline_url(url)
        assert decoded == content

    def test_decode_inline_url_non_inline(self):
        """Test decoding non-inline URL returns None."""
        assert _decode_inline_url("/local/card.js") is None


class TestDetectHaConfigYaml:
    """Cover the misroute-detection heuristic from #1072.

    LLMs sometimes emit HA-config YAML (`scene:`, `automation:`, ...) when
    they pick `ha_config_set_dashboard_resource` to "create a scene". The
    misroute lands as a Lovelace module containing YAML, creating orphaned
    HA entities. `_detect_ha_config_yaml` flags this shape early so the
    tool can reject with a pointer to the right config tool.
    """

    @pytest.mark.parametrize(
        "domain",
        [
            "automation",
            "script",
            "scene",
            "group",
            "input_boolean",
            "input_number",
            "input_select",
            "input_text",
            "input_datetime",
            "input_button",
            "template",
            "homeassistant",
            "sensor",
            "binary_sensor",
            "light",
            "switch",
        ],
    )
    def test_block_form_top_level_key_detected(self, domain):
        """Block-form `<domain>:` (newline after, no inline value) matches."""
        content = f"{domain}:\n  - id: foo\n    name: Foo\n"
        assert _detect_ha_config_yaml(content) == domain

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            # The exact theclue reproduction shape from #1072
            (
                "scene:\n  - id: 'luce_notturna_led_tavolo'\n    name: Luce Notturna",
                "scene",
            ),
            # Inline value form
            ("automation: !include automations.yaml\n", "automation"),
            # Block-string form
            ("template: |\n  {{ states('sensor.x') }}\n", "template"),
            # Flow-style sequence
            ("group: [foo, bar]\n", "group"),
            # Comment marker after key
            ("script:  # my scripts\n  greet:\n", "script"),
            # Whitespace-prefixed block (still detected after strip)
            ("\n\n  scene:\n  - id: x\n", "scene"),
        ],
    )
    def test_real_world_yaml_shapes(self, content, expected):
        """Realistic YAML shapes that smaller models emit get caught."""
        assert _detect_ha_config_yaml(content) == expected

    @pytest.mark.parametrize(
        "content",
        [
            # Legitimate ES6 module
            "export const config = { automation: 'foo' };",
            # CSS with selector that contains a marker substring
            ".automation { color: red; }",
            # JS with `automation` mentioned only in a comment
            "// automation: this is just a comment\nexport const x = 1;",
            # Word that starts with a marker but isn't a marker (`scenester:` ≠ `scene:`)
            "scenester: { foo: 'bar' };",
            # CSS @media rule
            "@media (max-width: 600px) { .x { display: none; } }",
            # JSON-style object literal
            '{ "automation": [], "scene": [] }',
            # Empty content (validated separately by the empty-content guard)
            "",
            "   \n\n  ",
            # Single-character starting line
            "x",
        ],
    )
    def test_legit_jscss_not_flagged(self, content):
        """JS/CSS that happens to mention marker words doesn't false-positive."""
        assert _detect_ha_config_yaml(content) is None


class TestHaConfigListDashboardResources:
    """Test ha_config_list_dashboard_resources tool."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def list_tool(self, mock_client):
        """Create ResourceTools instance and return the list method."""
        tools = ResourceTools(mock_client)
        return tools.ha_config_list_dashboard_resources

    @pytest.mark.asyncio
    async def test_list_empty(self, list_tool, mock_client):
        """Test listing when no resources exist."""
        mock_client.send_websocket_message.return_value = {"result": []}

        result = await list_tool()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["resources"] == []

    @pytest.mark.asyncio
    async def test_list_external_resources(self, list_tool, mock_client):
        """Test listing external resources."""
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": "1", "type": "module", "url": "/local/card.js"},
                {"id": "2", "type": "css", "url": "/local/theme.css"},
            ]
        }

        result = await list_tool()

        assert result["success"] is True
        assert result["count"] == 2
        assert result["by_type"]["module"] == 1
        assert result["by_type"]["css"] == 1

    @pytest.mark.asyncio
    async def test_list_inline_resources_decoded(self, list_tool, mock_client):
        """Test that inline resources are decoded with preview."""
        content = "export const myFunction = () => 'hello world';"
        encoded = base64.urlsafe_b64encode(content.encode()).decode()
        inline_url = f"{WORKER_BASE_URL}/{encoded}?type=module"

        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": inline_url}]
        }

        result = await list_tool()

        assert result["success"] is True
        assert result["inline_count"] == 1
        resource = result["resources"][0]
        assert resource["_inline"] is True
        assert resource["_size"] == len(content)
        assert resource["_preview"] == content
        assert resource["url"] == "[inline]"  # URL replaced

    @pytest.mark.asyncio
    async def test_list_inline_preview_truncated(self, list_tool, mock_client):
        """Test that long inline content preview is truncated."""
        content = "x" * 200
        encoded = base64.urlsafe_b64encode(content.encode()).decode()
        inline_url = f"{WORKER_BASE_URL}/{encoded}?type=module"

        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": inline_url}]
        }

        result = await list_tool()

        resource = result["resources"][0]
        assert resource["_preview"].endswith("...")
        assert len(resource["_preview"]) == 153  # 150 + "..."

    @pytest.mark.asyncio
    async def test_list_include_content_flag(self, list_tool, mock_client):
        """Test that include_content=True returns full content."""
        content = "x" * 200
        encoded = base64.urlsafe_b64encode(content.encode()).decode()
        inline_url = f"{WORKER_BASE_URL}/{encoded}?type=module"

        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": inline_url}]
        }

        result = await list_tool(include_content=True)

        resource = result["resources"][0]
        assert "_content" in resource
        assert resource["_content"] == content
        assert "_preview" not in resource  # Preview not included when content is


class TestHaConfigSetDashboardResource:
    """Test ha_config_set_dashboard_resource tool (inline and URL modes)."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def set_tool(self, mock_client):
        """Create ResourceTools instance and return the set method."""
        tools = ResourceTools(mock_client)
        return tools.ha_config_set_dashboard_resource

    # ---- Input validation ----

    @pytest.mark.asyncio
    async def test_neither_content_nor_url_error(self, set_tool, mock_client):
        """Test that providing neither content nor url returns error."""
        with pytest.raises(ToolError) as exc_info:
            await set_tool()

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "required" in error_data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_both_content_and_url_error(self, set_tool, mock_client):
        """Test that providing both content and url returns error."""
        with pytest.raises(ToolError) as exc_info:
            await set_tool(content="const x = 1;", url="/local/card.js")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "not both" in error_data["error"]["message"].lower()

    # ---- Inline content mode ----

    @pytest.mark.asyncio
    async def test_create_inline_module(self, set_tool, mock_client):
        """Test creating an inline module resource."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "new-id-123"}
        }

        content = "export const x = 1;"
        result = await set_tool(content=content, resource_type="module")

        assert result["success"] is True
        assert result["action"] == "created"
        assert result["resource_id"] == "new-id-123"
        assert result["resource_type"] == "module"
        assert result["size"] == len(content.encode("utf-8"))

        # Verify WebSocket call uses Cloudflare Worker URL
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/create"
        assert call_args["res_type"] == "module"
        assert WORKER_BASE_URL in call_args["url"]

    @pytest.mark.asyncio
    async def test_create_inline_css(self, set_tool, mock_client):
        """Test creating an inline CSS resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "css-123"}}

        result = await set_tool(content=".card { color: red; }", resource_type="css")

        assert result["success"] is True
        assert result["resource_type"] == "css"

    @pytest.mark.asyncio
    async def test_default_type_is_module(self, set_tool, mock_client):
        """Test that default resource_type is 'module'."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "123"}}

        await set_tool(content="const x = 1;")

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["res_type"] == "module"

    @pytest.mark.asyncio
    async def test_update_inline_resource(self, set_tool, mock_client):
        """Test updating an existing inline resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "existing"}}

        result = await set_tool(
            content="export const x = 2;",
            resource_type="module",
            resource_id="existing",
        )

        assert result["success"] is True
        assert result["action"] == "updated"
        assert result["resource_id"] == "existing"

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/update"
        assert call_args["resource_id"] == "existing"

    @pytest.mark.asyncio
    async def test_inline_empty_content_error(self, set_tool, mock_client):
        """Test that empty content returns error."""
        with pytest.raises(ToolError) as exc_info:
            await set_tool(content="")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "empty" in error_data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_inline_whitespace_only_error(self, set_tool, mock_client):
        """Test that whitespace-only content returns error."""
        with pytest.raises(ToolError) as exc_info:
            await set_tool(content="   \n\t  ")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "empty" in error_data["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_inline_content_too_large_error(self, set_tool, mock_client):
        """Test that oversized content returns error."""
        large_content = "x" * (MAX_CONTENT_SIZE + 1000)
        with pytest.raises(ToolError) as exc_info:
            await set_tool(content=large_content)

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "too large" in error_data["error"]["message"].lower()
        assert "suggestions" in error_data["error"]

    @pytest.mark.asyncio
    async def test_inline_js_type_not_supported(self, set_tool, mock_client):
        """Test that resource_type='js' is rejected for inline content."""
        with pytest.raises(ToolError) as exc_info:
            await set_tool(content="var x = 1;", resource_type="js")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "js" in error_data["error"]["message"].lower()

    # ---- HA-config YAML misroute rejection (#1072) ----

    @pytest.mark.asyncio
    async def test_inline_rejects_scene_yaml_with_995_pointer(
        self, set_tool, mock_client
    ):
        """Scene-YAML content is rejected with a pointer to issue #995.

        Reproduces the small-model misroute from #1072: model picks this
        tool to "create a scene" and passes scene-YAML as content. Without
        rejection, the YAML lands as a Lovelace module, not an HA scene.
        """
        scene_yaml = (
            "scene:\n"
            "  - id: 'luce_notturna_led_tavolo'\n"
            "    name: Luce Notturna\n"
            "    entities:\n"
            "      light.led_tavolo:\n"
            "        state: 'on'\n"
        )
        with pytest.raises(ToolError) as exc_info:
            await set_tool(content=scene_yaml, resource_type="module")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene:" in error_data["error"]["message"]
        assert "#1072" in error_data["error"]["message"]
        # `context=` fields are flattened to the top level by create_error_response
        assert error_data["detected_marker"] == "scene:"
        assert error_data["resource_type"] == "module"
        # Scene tools are tracked in #995; at least one suggestion must point there.
        suggestions = error_data["error"]["suggestions"]
        assert any("#995" in s for s in suggestions)
        # And no WebSocket call must have been made.
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_inline_rejects_automation_yaml_with_tool_pointer(
        self, set_tool, mock_client
    ):
        """`automation:` content is rejected with `ha_config_set_automation` suggestion."""
        with pytest.raises(ToolError) as exc_info:
            await set_tool(
                content="automation:\n  - id: morning\n    trigger: ...\n",
                resource_type="module",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "automation:" in error_data["error"]["message"]
        suggestions = error_data["error"]["suggestions"]
        assert any("ha_config_set_automation" in s for s in suggestions)
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_inline_rejects_input_boolean_yaml_with_helper_pointer(
        self, set_tool, mock_client
    ):
        """`input_boolean:` content is rejected with `ha_config_set_helper` suggestion."""
        with pytest.raises(ToolError) as exc_info:
            await set_tool(
                content="input_boolean:\n  guest_mode:\n    name: Guest\n",
                resource_type="module",
            )

        error_data = json.loads(str(exc_info.value))
        suggestions = error_data["error"]["suggestions"]
        assert any(
            "ha_config_set_helper" in s and "input_boolean" in s for s in suggestions
        )
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_inline_accepts_legit_module_mentioning_marker_words(
        self, set_tool, mock_client
    ):
        """Real ES6 module with marker words inside strings/comments is accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "legit-module-1"}
        }
        legit_content = (
            "// Card for displaying automation status\n"
            "export const automation_card = {\n"
            "  type: 'custom:automation-status',\n"
            "  scene: 'morning',\n"
            "};\n"
        )
        result = await set_tool(content=legit_content, resource_type="module")

        assert result["success"] is True
        assert result["action"] == "created"
        mock_client.send_websocket_message.assert_called()

    @pytest.mark.asyncio
    async def test_inline_accepts_css_with_selector_resembling_marker(
        self, set_tool, mock_client
    ):
        """CSS that uses `.automation { ... }` selectors is accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "legit-css-1"}
        }
        legit_css = ".automation, .scene { color: red; padding: 0; }\n"
        result = await set_tool(content=legit_css, resource_type="css")

        assert result["success"] is True
        assert result["action"] == "created"
        mock_client.send_websocket_message.assert_called()

    # ---- URL mode ----

    @pytest.mark.asyncio
    async def test_create_local_resource(self, set_tool, mock_client):
        """Test creating a local resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "local-1"}}

        result = await set_tool(url="/local/my-card.js", resource_type="module")

        assert result["success"] is True
        assert result["action"] == "created"
        assert result["url"] == "/local/my-card.js"

    @pytest.mark.asyncio
    async def test_create_hacs_resource(self, set_tool, mock_client):
        """Test creating a HACS resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "hacs-1"}}

        result = await set_tool(
            url="/hacsfiles/lovelace-mushroom/mushroom.js", resource_type="module"
        )

        assert result["success"] is True
        assert result["action"] == "created"

    @pytest.mark.asyncio
    async def test_update_url_resource(self, set_tool, mock_client):
        """Test updating an existing URL resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "existing"}}

        result = await set_tool(
            url="/local/card-v2.js", resource_type="module", resource_id="existing"
        )

        assert result["success"] is True
        assert result["action"] == "updated"

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/update"

    @pytest.mark.asyncio
    async def test_url_supports_js_type(self, set_tool, mock_client):
        """Test that legacy js type is supported for URL resources."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "123"}}

        result = await set_tool(url="/local/legacy.js", resource_type="js")

        assert result["success"] is True
        assert result["resource_type"] == "js"


class TestHaConfigDeleteDashboardResource:
    """Test ha_config_delete_dashboard_resource tool."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def delete_tool(self, mock_client):
        """Create ResourceTools instance and return the delete method."""
        tools = ResourceTools(mock_client)
        return tools.ha_config_delete_dashboard_resource

    @pytest.mark.asyncio
    async def test_delete_success(self, delete_tool, mock_client):
        """Test successful deletion."""
        mock_client.send_websocket_message.return_value = {"success": True}

        result = await delete_tool(resource_id="abc123")

        assert result["success"] is True
        assert result["action"] == "delete"
        assert result["resource_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_delete_not_found_raises_tool_error(self, delete_tool, mock_client):
        """Test that deleting non-existent resource raises ToolError with RESOURCE_NOT_FOUND."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Resource not found"},
        }

        with pytest.raises(ToolError) as exc_info:
            await delete_tool(resource_id="nonexistent")

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert result["error"]["code"] == "RESOURCE_NOT_FOUND"


class TestToolRegistration:
    """Test tool registration."""

    def test_registers_three_tools(self):
        """Test that exactly three tools are registered (list, set, delete)."""
        mcp = MagicMock()
        registered = []
        mcp.add_tool = lambda method: registered.append(method.__name__)

        register_resources_tools(mcp, MagicMock())

        assert "ha_config_list_dashboard_resources" in registered
        assert "ha_config_set_dashboard_resource" in registered
        assert "ha_config_delete_dashboard_resource" in registered
        assert "ha_config_set_inline_dashboard_resource" not in registered
        assert len(registered) == 3

    def test_set_tool_has_destructive_hint(self):
        """Test set tool has destructiveHint."""
        tools = ResourceTools(MagicMock())
        # Access the __fastmcp__ metadata set by @tool decorator
        metadata = tools.ha_config_set_dashboard_resource.__fastmcp__
        assert metadata.annotations.destructiveHint is True


class TestConstants:
    """Test module constants."""

    def test_worker_url_is_https(self):
        """Test worker URL uses HTTPS."""
        assert WORKER_BASE_URL.startswith("https://")

    def test_size_limits_reasonable(self):
        """Test size limits allow useful content."""
        assert MAX_CONTENT_SIZE >= 20000  # At least 20KB
        assert MAX_ENCODED_LENGTH >= 30000  # At least 30KB

    def test_base64_overhead_accounted(self):
        """Test content limit accounts for base64 expansion."""
        # Base64 increases size by 4/3
        expected_encoded = (MAX_CONTENT_SIZE * 4 + 2) // 3
        assert expected_encoded <= MAX_ENCODED_LENGTH
