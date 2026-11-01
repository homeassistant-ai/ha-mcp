"""E2E tests for patching inline dashboard resources with search/replace edits.

Exercises ``ha_config_set_dashboard_resource(edits=...)`` and
``ha_config_list_dashboard_resources(include_hash=True)`` against a real
Home Assistant: the edits travel through the MCP schema, the patched code is
what HA's registry actually stores, and a refused patch leaves it untouched.
"""

import hashlib
import json

from ...utilities.assertions import MCPAssertions, safe_call_tool
from .test_resources import _assert_stored_as_data_uri, _raw_resource_url

CARD_V1 = (
    "const LABEL = 'Wärme 🔥';\n"
    "const SPEED = 1.5;\n"
    "customElements.define('patch-e2e-card', class extends HTMLElement {});\n"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _listed_hash(mcp: MCPAssertions, resource_id: str) -> str | None:
    listing = await mcp.call_tool_success(
        "ha_config_list_dashboard_resources", {"include_hash": True}
    )
    row = next(r for r in listing["resources"] if r.get("id") == resource_id)
    return row.get("_sha256")


class TestPatchInlineResource:
    async def test_patch_updates_stored_code_and_listed_hash(
        self, mcp_client, ha_client
    ):
        """A hash-guarded patch lands in HA and the listed hash follows it."""
        mcp = MCPAssertions(mcp_client)
        created = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": CARD_V1, "resource_type": "module"},
        )
        resource_id = created["resource_id"]
        try:
            assert await _listed_hash(mcp, resource_id) == created["sha256"]

            patched = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "resource_id": resource_id,
                    "edits": [
                        {"old": "Wärme 🔥", "new": "Kälte ❄️"},
                        {"old": "SPEED = 1.5", "new": "SPEED = 2.5"},
                    ],
                    "expected_sha256": created["sha256"],
                },
            )

            expected = CARD_V1.replace("Wärme 🔥", "Kälte ❄️").replace("1.5", "2.5")
            await _assert_stored_as_data_uri(
                ha_client, resource_id, "data:text/javascript;base64,", expected
            )
            assert patched["action"] == "patched"
            assert patched["sha256"] == _sha(expected)
            assert await _listed_hash(mcp, resource_id) == patched["sha256"]
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_config_delete_dashboard_resource",
                {"resource_id": resource_id},
            )

    async def test_stringified_edits_from_clients_are_accepted(self, mcp_client):
        """Some MCP clients send list arguments as a JSON string."""
        mcp = MCPAssertions(mcp_client)
        created = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": CARD_V1, "resource_type": "module"},
        )
        resource_id = created["resource_id"]
        try:
            patched = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "resource_id": resource_id,
                    "edits": json.dumps([{"old": "1.5", "new": "1.75"}]),
                },
            )
            assert patched["applied"] == 1
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_config_delete_dashboard_resource",
                {"resource_id": resource_id},
            )

    async def test_refused_patch_leaves_registry_untouched(self, mcp_client, ha_client):
        """A stale hash or a failing edit must not change what HA serves."""
        mcp = MCPAssertions(mcp_client)
        created = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": CARD_V1, "resource_type": "module"},
        )
        resource_id = created["resource_id"]
        try:
            before = await _raw_resource_url(ha_client, resource_id)

            stale = await mcp.call_tool_failure(
                "ha_config_set_dashboard_resource",
                {
                    "resource_id": resource_id,
                    "edits": [{"old": "1.5", "new": "1.75"}],
                    "expected_sha256": _sha("an older version"),
                },
            )
            partial = await mcp.call_tool_failure(
                "ha_config_set_dashboard_resource",
                {
                    "resource_id": resource_id,
                    "edits": [
                        {"old": "1.5", "new": "1.75"},
                        {"old": "absent text", "new": "x"},
                    ],
                },
            )

            assert stale["reason"] == "hash_mismatch"
            assert partial["reason"] == "old_not_found"
            assert await _raw_resource_url(ha_client, resource_id) == before
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_config_delete_dashboard_resource",
                {"resource_id": resource_id},
            )

    async def test_url_resource_cannot_be_patched(self, mcp_client):
        """Edits only apply to inline code; /local/ files are refused by name."""
        mcp = MCPAssertions(mcp_client)
        created = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"url": "/local/patch-e2e-card.js", "resource_type": "module"},
        )
        resource_id = created["resource_id"]
        try:
            refused = await mcp.call_tool_failure(
                "ha_config_set_dashboard_resource",
                {"resource_id": resource_id, "edits": [{"old": "a", "new": "b"}]},
            )
            assert refused["reason"] == "not_inline"
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_config_delete_dashboard_resource",
                {"resource_id": resource_id},
            )
