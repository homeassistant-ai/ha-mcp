"""E2E tests for patching inline dashboard resources with search/replace edits.

Exercises ``ha_config_set_dashboard_resource(edits=...)`` and
``ha_config_list_dashboard_resources(include_hash=True)`` against a real
Home Assistant: the edits travel through the MCP schema and the patched code
is what HA's registry actually stores. Refusals are covered by unit tests.
"""

import hashlib

from ...utilities.assertions import MCPAssertions, safe_call_tool
from .test_resources import _assert_stored_as_data_uri

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
    return row.get("_config_hash")


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
            assert await _listed_hash(mcp, resource_id) == created["config_hash"]

            patched = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "resource_id": resource_id,
                    "edits": [
                        {"old": "Wärme 🔥", "new": "Kälte ❄️"},
                        {"old": "SPEED = 1.5", "new": "SPEED = 2.5"},
                    ],
                    "config_hash": created["config_hash"],
                },
            )

            expected = CARD_V1.replace("Wärme 🔥", "Kälte ❄️").replace("1.5", "2.5")
            await _assert_stored_as_data_uri(
                ha_client, resource_id, "data:text/javascript;base64,", expected
            )
            assert patched["action"] == "patched"
            assert patched["config_hash"] == _sha(expected)
            assert await _listed_hash(mcp, resource_id) == patched["config_hash"]
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_config_delete_dashboard_resource",
                {"resource_id": resource_id},
            )
