"""E2E: ha_set_integration(domain=..., describe=True) reads a setup form.

describe=True describes the domain's config flow instead of adding it: Core
starts it, the tool describes the first step (or the form behind
menu_choice) and aborts it. A flow Core aborts comes back with Core's own
reason text, read here from Core's translations the way the frontend looks
it up (the abort's translation_domain, else the handler). With an entry_id,
include_subentry_schema describes a config subentry flow the same way.
"""

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.forecast_solar import create_forecast_solar_entry


async def _config_strings(
    ha_client, domain: str, category: str = "config"
) -> dict[str, str]:
    reply = await ha_client.send_websocket_message(
        {
            "type": "frontend/get_translations",
            "language": "en",
            "category": category,
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
                "ha_set_integration", {"domain": "group", "describe": True}
            )
            light = await mcp.call_tool_success(
                "ha_set_integration",
                {"domain": "group", "describe": True, "menu_choice": "light"},
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
                "ha_set_integration", {"domain": "sun", "describe": True}
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

    async def test_unknown_domain_fails_the_describe(self, mcp_client):
        """A domain Core has no config flow for has no setup form to describe."""
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_set_integration",
                {"domain": "no_such_integration_xyz", "describe": True},
                expected_error="no_such_integration_xyz",
            )

    @pytest.mark.slow
    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    async def test_subentry_schema_gives_cores_abort_text_and_the_reconfigure_form(
        self, mcp_client, ha_client
    ):
        """Forecast.Solar: a second plane needs an API key, so Core aborts that
        subentry flow; the existing plane's reconfigure flow opens with a form."""
        flow_done = await create_forecast_solar_entry(ha_client)
        assert flow_done.get("type") == "create_entry", flow_done
        entry_id = flow_done["result"]["entry_id"]
        try:
            strings = await _config_strings(
                ha_client, "forecast_solar", category="config_subentries"
            )
            reason = "api_key_required"
            expected = strings[
                f"component.forecast_solar.config_subentries.plane.abort.{reason}"
            ]
            async with MCPAssertions(mcp_client) as mcp:
                listed = await mcp.call_tool_success(
                    "ha_get_integration",
                    {"entry_id": entry_id, "include_subentries": True},
                )
                planes = [
                    s for s in listed["subentries"] if s.get("subentry_type") == "plane"
                ]
                assert len(planes) == 1, listed
                second_plane = await mcp.call_tool_success(
                    "ha_get_integration",
                    {
                        "entry_id": entry_id,
                        "include_subentry_schema": True,
                        "subentry_type": "plane",
                    },
                )
                existing_plane = await mcp.call_tool_success(
                    "ha_get_integration",
                    {
                        "entry_id": entry_id,
                        "include_subentry_schema": True,
                        "subentry_type": "plane",
                        "subentry_id": planes[0]["subentry_id"],
                    },
                )
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

        add_schema = second_plane["subentry_schema"]
        assert add_schema["aborted"] == reason
        assert add_schema["reason_text"] == expected
        assert "fields" not in add_schema
        edit_schema = existing_plane["subentry_schema"]
        assert edit_schema["flow_type"] == "form"
        assert edit_schema["step_id"] == "reconfigure"
        assert edit_schema["fields"]
