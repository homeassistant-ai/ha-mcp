"""E2E: ha_get_integration(domain=..., include_schema=True) reads a setup form.

Without an entry_id, include_schema describes the domain's config flow: Core
starts it, the tool describes the first step (or the form behind
menu_choice) and aborts it. A flow Core aborts comes back with Core's own
reason text, read here from Core's translations the way the frontend looks
it up (the abort's translation_domain, else the handler).
"""

import pytest

from ...utilities.assertions import MCPAssertions


async def _config_strings(ha_client, domain: str) -> dict[str, str]:
    reply = await ha_client.send_websocket_message(
        {
            "type": "frontend/get_translations",
            "language": "en",
            "category": "config",
            "integration": [domain],
        }
    )
    return reply["result"]["resources"]


@pytest.mark.asyncio
@pytest.mark.integrations
class TestIntegrationSetupSchema:
    async def test_group_setup_menu_then_the_light_form(self, mcp_client, ha_client):
        """A menu-first setup flow lists its branches; menu_choice opens one."""
        strings = await _config_strings(ha_client, "group")
        expected_all = strings["component.group.config.step.light.data_description.all"]

        async with MCPAssertions(mcp_client) as mcp:
            menu = await mcp.call_tool_success(
                "ha_get_integration", {"domain": "group", "include_schema": True}
            )
            light = await mcp.call_tool_success(
                "ha_get_integration",
                {"domain": "group", "include_schema": True, "menu_choice": "light"},
            )

        assert menu["config_flow_schema"]["flow_type"] == "menu"
        assert "light" in menu["config_flow_schema"]["menu_options"]
        schema = light["config_flow_schema"]
        assert schema["flow_type"] == "form"
        assert schema["menu_choice"] == "light"
        fields = {f["name"]: f for f in schema["fields"]}
        assert "entities" in fields
        assert fields["all"]["description"] == expected_all

    async def test_single_instance_setup_flow_returns_cores_abort_text(
        self, mcp_client, ha_client
    ):
        """sun is already set up in the test instance, so Core aborts its flow."""
        async with MCPAssertions(mcp_client) as mcp:
            entries = await mcp.call_tool_success(
                "ha_get_integration", {"domain": "sun"}
            )
            assert entries["entries"], "the test instance must have a sun entry"
            data = await mcp.call_tool_success(
                "ha_get_integration", {"domain": "sun", "include_schema": True}
            )

        # Core's own abort for the same request names where its text lives.
        core_abort = await ha_client.start_config_flow("sun")
        assert core_abort["type"] == "abort"
        text_domain = core_abort.get("translation_domain") or "sun"
        strings = await _config_strings(ha_client, text_domain)
        expected = strings[
            f"component.{text_domain}.config.abort.{core_abort['reason']}"
        ]

        schema = data["config_flow_schema"]
        assert schema["aborted"] == core_abort["reason"]
        assert schema["reason_text"] == expected
        assert "fields" not in schema

    async def test_unknown_domain_is_a_warning(self, mcp_client):
        """A domain Core has no config flow for does not fail the listing."""
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_get_integration",
                {"domain": "no_such_integration_xyz", "include_schema": True},
            )

        assert "config_flow_schema" not in data
        assert any("no_such_integration_xyz" in w for w in data["warnings"])
