"""Unit tests for dashboard resource tools."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools import tools_resources
from ha_mcp.tools.tools_resources import (
    _ALLOWED_DATA_URI_PREFIXES,
    LEGACY_WORKER_BASE_URL,
    MAX_CONTENT_SIZE,
    ResourceTools,
    _data_uri_for,
    _decode_data_uri,
    _decode_inline_content,
    _decode_legacy_worker_url,
    _detect_ha_config_yaml,
    _is_data_url,
    register_resources_tools,
)


def _legacy_url(content: str, resource_type: str = "module") -> str:
    """Build a legacy worker inline URL the way the retired writer did."""
    encoded = base64.urlsafe_b64encode(content.encode()).decode()
    return f"{LEGACY_WORKER_BASE_URL}/{encoded}?type={resource_type}"


class TestHelperFunctions:
    """Test helper functions."""

    def test_data_uri_roundtrip(self):
        """Content encodes to a data: URI that decodes back byte-identical."""
        content = "export const x = 'ünïcode';"
        url = _data_uri_for(content, "module")

        assert url.startswith("data:text/javascript;base64,")
        assert _decode_data_uri(url) == content

    def test_data_uri_css_roundtrip_with_charset(self):
        """CSS gets an explicit UTF-8 charset and round-trips non-ASCII."""
        content = '.card::before { content: "ünïcode ✓"; }'
        url = _data_uri_for(content, "css")

        assert url.startswith("data:text/css;charset=utf-8;base64,")
        assert _decode_data_uri(url) == content

    def test_data_uri_mime_by_type(self):
        """module → text/javascript, css → text/css with charset."""
        assert _data_uri_for(".x {}", "css").startswith(
            "data:text/css;charset=utf-8;base64,"
        )
        assert _data_uri_for("1;", "module").startswith("data:text/javascript;base64,")

    def test_data_uri_deterministic(self):
        """Same content always maps to the same URL."""
        assert _data_uri_for("const a = 1;", "module") == _data_uri_for(
            "const a = 1;", "module"
        )

    def test_data_uri_roundtrip_at_max_size(self):
        """A payload at exactly MAX_CONTENT_SIZE survives the round-trip."""
        content = "x" * MAX_CONTENT_SIZE
        assert _decode_data_uri(_data_uri_for(content, "module")) == content

    def test_decode_data_uri_rejects_non_data(self):
        """Non-data: URLs decode to None."""
        assert _decode_data_uri("/local/card.js") is None
        assert _decode_data_uri("https://cdn.example.com/card.js") is None

    def test_decode_data_uri_rejects_percent_encoded_form(self):
        """Percent-encoded (non-base64) data: URIs are not claimed as ours."""
        assert _decode_data_uri("data:text/javascript,console.log(1)") is None

    def test_decode_data_uri_rejects_foreign_media_type(self):
        """data: URLs with media types we never write are not claimed."""
        assert _decode_data_uri("data:image/png;base64,aGVsbG8=") is None
        assert _decode_data_uri("data:text/html;base64,aGVsbG8=") is None

    def test_decode_data_uri_rejects_corrupt_payload(self):
        """Non-alphabet bytes in the payload decode to None, not silently
        truncated content — the browser's forgiving-base64 rejects them, so
        reporting 'valid' content would point diagnosis away from the cause."""
        good = _data_uri_for("console.log(1)", "module")
        assert _decode_data_uri(good + "!!junktail") is None

    def test_decode_data_uri_scheme_case_insensitive(self):
        """Scheme/media type match case-insensitively; payload case preserved."""
        content = "const CasePreserved = 1;"
        url = _data_uri_for(content, "module")
        upper_head = url[: len("data:text/javascript;base64,")].upper()
        payload = url[len("data:text/javascript;base64,") :]
        assert _decode_data_uri(upper_head + payload) == content

    def test_decode_inline_content_data_uri(self):
        """Our data: URIs decode as non-legacy inline content."""
        content = "export const x = 1;"
        assert _decode_inline_content(_data_uri_for(content, "module")) == (
            content,
            False,
        )
        assert _decode_inline_content(_data_uri_for(".x {}", "css")) == (".x {}", False)

    def test_decode_inline_content_legacy_worker(self):
        """Legacy worker URLs decode and are flagged as legacy."""
        content = "export const legacy = 1;"
        assert _decode_inline_content(_legacy_url(content)) == (content, True)

    def test_decode_inline_content_non_inline(self):
        """Foreign and undecodable URLs yield no content.

        This is the single source of truth for the list tool's inline_count
        AND its per-resource markers, so anything returning None here must
        also be left unmarked and uncounted.
        """
        for url in (
            "/local/card.js",
            "https://cdn.example.com/card.js",
            "data:image/png;base64,aGVsbG8=",
            "data:text/javascript,console.log(1)",
            "data:text/css;charset=utf-8;base64,",
            "data:text/javascript;base64,!!not-base64!!",
        ):
            assert _decode_inline_content(url) is None, url

    def test_decode_inline_content_lookalike_host(self):
        """Detection is anchored — lookalike hosts embedding the worker host
        string are neither decoded nor flagged."""
        lookalike = LEGACY_WORKER_BASE_URL.replace(
            "https://", "https://evil.example.com/"
        )
        assert _decode_inline_content(f"{lookalike}/YWJj?type=module") is None
        suffixed = f"{LEGACY_WORKER_BASE_URL}.evil.example.com/YWJj"
        assert _decode_inline_content(suffixed) is None

    def test_is_data_url_normalizes_like_a_browser(self):
        """Leading C0/space and embedded tab/newline are stripped before the
        scheme check, matching WHATWG URL parsing — otherwise a padded
        data: URL slips past the url= guard and still loads in a browser."""
        assert _is_data_url("data:text/javascript;base64,YWJj") is True
        assert _is_data_url("DATA:text/javascript;base64,YWJj") is True
        assert _is_data_url(" data:text/javascript,x") is True
        assert _is_data_url("\tdata:text/javascript,x") is True
        assert _is_data_url("\n data:text/javascript,x") is True
        assert _is_data_url("\x00data:text/javascript,x") is True
        assert _is_data_url("da\tta:text/javascript,x") is True
        assert _is_data_url("/local/card.js") is False
        assert _is_data_url("https://cdn.example.com/data:x") is False

    @pytest.mark.parametrize("prefix", _ALLOWED_DATA_URI_PREFIXES)
    def test_decode_data_uri_accepts_every_generated_prefix(self, prefix):
        """All four accepted prefixes decode, including the two read-compat
        forms the writer never emits (js-with-charset, css-without).

        Dropping them as 'dead' would silently stop recognizing resources
        written by a differently-versioned ha-mcp: they would lose _inline,
        fall out of inline_count, and — critically — escape the truncated
        preview guard, so an agent could overwrite them with a preview.
        """
        import base64 as _b64

        content = "export const x = 1;"
        url = prefix + _b64.b64encode(content.encode()).decode()
        assert _decode_data_uri(url) == content

    def test_decode_data_uri_rejects_invalid_utf8(self):
        """Valid base64 that is not valid UTF-8 decodes to None.

        Distinct code path from the corrupt-alphabet case (UnicodeDecodeError
        rather than binascii.Error).
        """
        import base64 as _b64

        payload = _b64.b64encode(bytes([255, 254, 0]) + b"abc").decode()
        assert _decode_data_uri("data:text/javascript;base64," + payload) is None

    def test_decode_legacy_worker_url_rejects_corrupt_payload(self):
        """The legacy decoder is as strict as the data: one.

        urlsafe_b64decode silently DISCARDS non-alphabet characters, so
        without an explicit check a junk path could decode to plausible text
        and be masked as inline content.
        """
        assert (
            _decode_legacy_worker_url(f"{LEGACY_WORKER_BASE_URL}/$$$$aGVsbG8=") is None
        )
        assert (
            _decode_legacy_worker_url(f"{LEGACY_WORKER_BASE_URL}/some-random-path")
            is None
        )

    def test_decode_legacy_worker_url_case_insensitive_origin(self):
        """Scheme/host case does not lose a legacy resource its migration path."""
        content = "export const legacy = 1;"
        url = _legacy_url(content)
        shouty = url.replace("https://", "HTTPS://", 1)
        assert _decode_legacy_worker_url(shouty) == content

    def test_decode_legacy_worker_url(self):
        """Test decoding legacy worker URL (pure local base64)."""
        content = "export const x = 1;"
        assert _decode_legacy_worker_url(_legacy_url(content)) == content

    def test_decode_legacy_worker_url_non_worker(self):
        """Test decoding non-worker URL returns None."""
        assert _decode_legacy_worker_url("/local/card.js") is None


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
            # BOM-prefixed YAML (e.g. emitted by tools that always BOM-mark UTF-8)
            ("\ufeffscene:\n  - id: foo\n    name: Foo\n", "scene"),
            # File-leading comment line precedes the real first key
            ("# Generated by my tool\nautomation:\n  - id: morning\n", "automation"),
            # YAML document-start marker precedes the real first key
            ("---\nautomation:\n  - id: morning\n", "automation"),
            # Combined decoration: BOM + comment + doc-start before the key
            ("\ufeff# header\n---\nscene:\n  - id: x\n", "scene"),
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
        """Test that data: URI inline resources are decoded with preview."""
        content = "export const myFunction = () => 'hello world';"
        inline_url = _data_uri_for(content, "module")

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
        assert "_legacy_worker" not in resource

    @pytest.mark.asyncio
    async def test_list_legacy_worker_resources_flagged(self, list_tool, mock_client):
        """Legacy worker resources decode locally and carry a migration hint."""
        content = "export const legacy = true;"
        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": _legacy_url(content)}]
        }

        result = await list_tool()

        assert result["inline_count"] == 1
        resource = result["resources"][0]
        assert resource["_inline"] is True
        assert resource["_preview"] == content
        assert resource["_legacy_worker"] is True
        assert resource["url"] == "[inline]"
        # The hint is emitted ONCE per response, not repeated per resource,
        # and MUST route agents through include_content=True — the default
        # response only carries the truncated _preview, and re-saving that
        # would destroy the resource.
        assert "_migration" not in resource
        assert "include_content=True" in result["migration_hint"]
        assert "_preview" in result["migration_hint"]

    @pytest.mark.asyncio
    async def test_list_inline_preview_truncated(self, list_tool, mock_client):
        """Test that long inline content preview is truncated."""
        content = "x" * 200
        inline_url = _data_uri_for(content, "module")

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
        inline_url = _data_uri_for(content, "module")

        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": inline_url}]
        }

        result = await list_tool(include_content=True)

        resource = result["resources"][0]
        assert "_content" in resource
        assert resource["_content"] == content
        assert "_preview" not in resource  # Preview not included when content is

    @pytest.mark.asyncio
    async def test_list_size_is_bytes(self, list_tool, mock_client):
        """_size reports UTF-8 bytes, matching the set response's size field."""
        content = "const s = 'ünïcode';"
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": "1", "type": "module", "url": _data_uri_for(content, "module")}
            ]
        }

        result = await list_tool()

        assert result["resources"][0]["_size"] == len(content.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_list_foreign_data_uri_passes_through(self, list_tool, mock_client):
        """Foreign data: resources keep their real URL and are not claimed."""
        url = "data:image/png;base64,aGVsbG8="
        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "1", "type": "module", "url": url}]
        }

        result = await list_tool()

        resource = result["resources"][0]
        assert resource["url"] == url
        assert "_inline" not in resource
        assert result["inline_count"] == 0

    @pytest.mark.asyncio
    async def test_list_undecodable_inline_passes_through(self, list_tool, mock_client):
        """Empty or corrupt payloads keep their real URL visible.

        Masking them as "[inline]" would hide exactly what a user debugging
        a broken resource needs to see.
        """
        empty = "data:text/css;charset=utf-8;base64,"
        corrupt = "data:text/javascript;base64,!!not-base64!!"
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": "1", "type": "css", "url": empty},
                {"id": "2", "type": "module", "url": corrupt},
            ]
        }

        result = await list_tool()

        assert result["resources"][0]["url"] == empty
        assert "_inline" not in result["resources"][0]
        assert result["resources"][1]["url"] == corrupt
        assert "_inline" not in result["resources"][1]
        # The summary must agree with the markers — this is the ONLY input
        # shape where a decode-based count and a URL-shape count differ, so
        # it is what pins the "cannot disagree" invariant.
        assert result["inline_count"] == 0
        # Recognized as ours but undecodable: say so, or a user debugging a
        # dead card reads it as someone else's resource.
        assert result["resources"][0]["_decode_error"] is True
        assert result["resources"][1]["_decode_error"] is True

    @pytest.mark.asyncio
    async def test_list_decodes_only_the_returned_page(
        self, list_tool, mock_client, monkeypatch
    ):
        """Content is materialized for the requested page only, while
        inline_count still summarizes the full set — include_content=True
        responses stay bounded by limit/offset."""
        contents = [f"export const page_test_{i} = {i};" for i in range(3)]
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": str(i), "type": "module", "url": _data_uri_for(c, "module")}
                for i, c in enumerate(contents)
            ]
        }

        seen_page_sizes = []
        real_process = tools_resources._process_resource_list

        def _spy(page, decoded_page, include):
            seen_page_sizes.append(len(page))
            return real_process(page, decoded_page, include)

        monkeypatch.setattr(tools_resources, "_process_resource_list", _spy)

        result = await list_tool(include_content=True, limit=1, offset=1)

        assert result["total_count"] == 3
        assert result["inline_count"] == 3
        assert len(result["resources"]) == 1
        assert result["resources"][0]["_content"] == contents[1]
        # The probe is what actually pins the behavior: reverting to
        # "process everything, then slice" would still satisfy the
        # assertions above, but would hand the renderer all 3 records.
        assert seen_page_sizes == [1]

    @pytest.mark.asyncio
    async def test_list_content_budget_flags_rather_than_shortens(
        self, list_tool, mock_client
    ):
        """Past the per-response content budget, rows are FLAGGED not cut.

        Handing back a silently-shortened payload is exactly how a resource
        gets destroyed when an agent writes it back, so the budget must
        never produce a partial _content value.
        """
        big = "q" * 200_000
        mock_client.send_websocket_message.return_value = {
            "result": [
                {
                    "id": str(i),
                    "type": "module",
                    "url": _data_uri_for(big + str(i), "module"),
                }
                for i in range(4)
            ]
        }

        result = await list_tool(include_content=True)

        emitted = [r for r in result["resources"] if "_content" in r]
        flagged = [r for r in result["resources"] if r.get("_content_truncated")]
        assert emitted and flagged, "expected some emitted and some flagged"
        assert len(emitted) + len(flagged) == 4
        # Nothing partial: every emitted value is the whole resource.
        for row in emitted:
            assert row["_content"].startswith(big)
        for row in flagged:
            assert "_content" not in row
        assert result["content_truncated"] == len(flagged)
        assert "limit=1" in result["content_truncation_note"]


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

        # Registered URL is a self-contained data: URI holding exactly the
        # submitted content — nothing external involved.
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/create"
        assert call_args["res_type"] == "module"
        assert call_args["url"].startswith("data:text/javascript;base64,")
        assert _decode_data_uri(call_args["url"]) == content

    @pytest.mark.asyncio
    async def test_create_inline_css(self, set_tool, mock_client):
        """Test creating an inline CSS resource."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "css-123"}}

        result = await set_tool(content=".card { color: red; }", resource_type="css")

        assert result["success"] is True
        assert result["resource_type"] == "css"

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["url"].startswith("data:text/css;charset=utf-8;base64,")
        assert _decode_data_uri(call_args["url"]) == ".card { color: red; }"

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

        # Updates always write the current data: URI form — updating a
        # legacy worker-hosted resource with new content migrates it off
        # the legacy worker as a side effect.
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/update"
        assert call_args["resource_id"] == "existing"
        assert call_args["url"].startswith("data:text/javascript;base64,")

    @pytest.mark.asyncio
    async def test_update_rejects_truncated_preview_resave(self, set_tool, mock_client):
        """Re-saving the list tool's truncated _preview as content is refused.

        The default list response carries only the 150-char preview + "...";
        an agent that writes that back would destroy the user's card.
        """
        full = "x" * 200
        preview = full[:150] + "..."
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": "res1", "type": "module", "url": _data_uri_for(full, "module")}
            ]
        }

        with pytest.raises(ToolError) as exc_info:
            await set_tool(content=preview, resource_type="module", resource_id="res1")

        error_data = json.loads(str(exc_info.value))
        assert "truncated" in error_data["error"]["message"]
        suggestions = error_data["error"]["suggestions"]
        assert any("include_content=True" in s for s in suggestions)
        # Only the guard's listing call ran — no update was sent.
        assert mock_client.send_websocket_message.call_count == 1
        sent = mock_client.send_websocket_message.call_args[0][0]
        assert sent["type"] == "lovelace/resources"

    @pytest.mark.asyncio
    async def test_update_rejects_truncated_preview_of_legacy_resource(
        self, set_tool, mock_client
    ):
        """The preview guard also protects legacy worker-hosted resources."""
        full = "y" * 300
        preview = full[:150] + "..."
        mock_client.send_websocket_message.return_value = {
            "result": [{"id": "res1", "type": "module", "url": _legacy_url(full)}]
        }

        with pytest.raises(ToolError):
            await set_tool(content=preview, resource_type="module", resource_id="res1")

        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_short_legit_update_skips_the_preview_check(
        self, set_tool, mock_client
    ):
        """Ordinary short content must not trigger the guard at all.

        The guard fails closed, so matching routine short updates would
        make them fail whenever a listing hiccups — for content that
        cannot be a preview of anything.
        """
        mock_client.send_websocket_message.return_value = {"result": {"id": "existing"}}

        result = await set_tool(
            content="/* tweak */", resource_type="css", resource_id="existing"
        )

        assert result["success"] is True
        # One call only: the update. No verification listing was needed.
        assert mock_client.send_websocket_message.call_count == 1
        assert (
            mock_client.send_websocket_message.call_args[0][0]["type"]
            == "lovelace/resources/update"
        )

    @pytest.mark.asyncio
    async def test_preview_guard_fails_closed_when_listing_raises(
        self, set_tool, mock_client
    ):
        """An unverifiable preview-shaped write is REFUSED, not allowed.

        A timeout on the (now large) listing does not imply the connection
        is dead, so the small upsert frame right after would succeed and
        destroy the card while reporting success.
        """
        mock_client.send_websocket_message.side_effect = TimeoutError("no answer")

        with pytest.raises(ToolError) as exc_info:
            await set_tool(
                content="x" * 150 + "...", resource_type="module", resource_id="res1"
            )

        error_data = json.loads(str(exc_info.value))
        assert "could not be read back" in error_data["error"]["message"]
        # Exactly one call — the listing. No update was attempted.
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_preview_guard_fails_closed_on_ws_error_envelope(
        self, set_tool, mock_client
    ):
        """An HA-side rejection of the listing also fails closed."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "unauthorized"},
        }

        with pytest.raises(ToolError) as exc_info:
            await set_tool(
                content="x" * 150 + "...", resource_type="module", resource_id="res1"
            )

        assert "unauthorized" in str(exc_info.value)
        assert mock_client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_preview_guard_survives_malformed_listing_entries(
        self, set_tool, mock_client
    ):
        """A non-dict entry must not escape as a bare AttributeError."""
        full = "y" * 400
        mock_client.send_websocket_message.side_effect = [
            {
                "result": [
                    "not-a-dict",
                    None,
                    {
                        "id": "res1",
                        "type": "module",
                        "url": _data_uri_for(full, "module"),
                    },
                ]
            },
            {"result": {"id": "res1"}},
        ]

        with pytest.raises(ToolError) as exc_info:
            await set_tool(
                content=full[:150] + "...", resource_type="module", resource_id="res1"
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_preview_guard_catches_ellipsis_stripped_preview(
        self, set_tool, mock_client
    ):
        """The guard tolerates an agent trimming the ellipsis or whitespace."""
        full = "z" * 500
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": "res1", "type": "module", "url": _data_uri_for(full, "module")}
            ]
        }

        with pytest.raises(ToolError):
            await set_tool(
                content=full[:150], resource_type="module", resource_id="res1"
            )

    @pytest.mark.asyncio
    async def test_update_allows_legit_content_shaped_like_preview(
        self, set_tool, mock_client
    ):
        """153-char content ending in '...' that is NOT the stored preview saves."""
        legit = "a" * 150 + "..."
        stored = "completely different stored content " * 10
        mock_client.send_websocket_message.side_effect = [
            {
                "result": [
                    {
                        "id": "res1",
                        "type": "module",
                        "url": _data_uri_for(stored, "module"),
                    }
                ]
            },
            {"result": {"id": "res1"}},
        ]

        result = await set_tool(
            content=legit, resource_type="module", resource_id="res1"
        )

        assert result["success"] is True
        assert result["action"] == "updated"

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
    async def test_inline_size_is_utf8_bytes_not_characters(
        self, set_tool, mock_client
    ):
        """size (and the cap) are measured in UTF-8 bytes, not characters.

        Every other size test uses ASCII, where the two are identical — so
        a refactor to len(content) would pass them all while letting a
        multibyte payload up to 4x over the cap through.
        """
        mock_client.send_websocket_message.return_value = {"result": {"id": "1"}}
        content = chr(233) * 100  # 100 chars, 200 UTF-8 bytes

        result = await set_tool(content=content, resource_type="module")

        assert result["size"] == len(content.encode("utf-8")) == 200
        assert result["size"] != len(content)

    @pytest.mark.asyncio
    async def test_inline_cap_counts_bytes_not_characters(self, set_tool, mock_client):
        """Content under the cap in characters but over it in bytes is rejected."""
        content = chr(233) * (MAX_CONTENT_SIZE // 2 + 1)  # 2 bytes each -> over

        with pytest.raises(ToolError) as exc_info:
            await set_tool(content=content, resource_type="module")

        assert len(content) < MAX_CONTENT_SIZE  # would pass a character check
        error_data = json.loads(str(exc_info.value))
        assert "too large" in error_data["error"]["message"].lower()
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_inline_cap_boundary(self, set_tool, mock_client):
        """Exactly MAX_CONTENT_SIZE is accepted; one byte over is rejected."""
        mock_client.send_websocket_message.return_value = {"result": {"id": "1"}}

        at_cap = "x" * MAX_CONTENT_SIZE
        result = await set_tool(content=at_cap, resource_type="module")
        assert result["success"] is True
        assert result["size"] == MAX_CONTENT_SIZE

        with pytest.raises(ToolError):
            await set_tool(content="x" * (MAX_CONTENT_SIZE + 1), resource_type="module")

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
    async def test_inline_update_rejects_scene_yaml_with_995_pointer(
        self, set_tool, mock_client
    ):
        """Update path (resource_id provided) also rejects scene-YAML.

        Locks the YAML check above ``_upsert_resource`` — a future refactor
        moving the reject below the WebSocket call would silently overwrite
        an existing resource with malformed YAML and the suite would stay
        green without this test.
        """
        scene_yaml = (
            "scene:\n  - id: 'luce_notturna_led_tavolo'\n    name: Luce Notturna\n"
        )
        with pytest.raises(ToolError) as exc_info:
            await set_tool(
                content=scene_yaml,
                resource_type="module",
                resource_id="existing-resource-id",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "scene:" in error_data["error"]["message"]
        suggestions = error_data["error"]["suggestions"]
        assert any("#995" in s for s in suggestions)
        # Critical: no upsert call with the malformed payload.
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

    @pytest.mark.asyncio
    async def test_url_mode_rejects_data_urls(self, set_tool, mock_client):
        """Hand-built data: URLs can't bypass the inline-mode guards via url=.

        url= runs none of the inline validations (empty/size/type checks and
        the #1072 YAML-misroute rejection), so a documented data: format
        registered through it would be a guard bypass.
        """
        with pytest.raises(ToolError) as exc_info:
            await set_tool(url="data:text/javascript;base64,eA==")

        error_data = json.loads(str(exc_info.value))
        assert "content=" in error_data["error"]["message"]
        mock_client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_url_mode_rejects_data_urls_any_case_and_type(
        self, set_tool, mock_client
    ):
        """The data: rejection is scheme-case-insensitive and mime-agnostic."""
        with pytest.raises(ToolError):
            await set_tool(url="DATA:text/javascript;base64,eA==")
        with pytest.raises(ToolError):
            await set_tool(url="data:image/png;base64,eA==", resource_type="js")
        mock_client.send_websocket_message.assert_not_called()


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

    def test_max_content_size_pinned(self):
        """Pin the cap at 128KB — sized to fit established single-file card
        bundles. The registered URL ships to every browser on each dashboard
        load and auto-backup snapshots it whole per edit, so changing this
        needs a deliberate decision."""
        assert MAX_CONTENT_SIZE == 128_000
