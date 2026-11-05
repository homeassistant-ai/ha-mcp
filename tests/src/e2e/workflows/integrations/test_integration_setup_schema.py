"""E2E: ha_set_integration(domain=...) without config reads a setup form.

A new flow without config starts the domain's config flow and submits
nothing: the first step comes back open with its flow_id (the form's fields,
or a menu's menu_options), and flow_id with menu_choice or abort=True
continues or ends it. A flow Core aborts raises with Core's own reason text,
read here from Core's translations the way the frontend looks it up (the
abort's translation_domain, else the handler). An entry_id alone opens the
entry's options flow the same way; with subentry_type it describes (and
aborts) a config subentry flow.
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
        """A menu-first setup flow lists its branches; menu_choice with the
        flow_id opens one, one step per call."""
        strings = await _config_strings(ha_client, "group")
        expected_all = strings["component.group.config.step.light.data_description.all"]

        flow_id = None
        try:
            async with MCPAssertions(mcp_client) as mcp:
                menu = await mcp.call_tool_success(
                    "ha_set_integration", {"domain": "group"}
                )
                flow_id = menu["flow_id"]
                light = await mcp.call_tool_success(
                    "ha_set_integration",
                    {"domain": "group", "flow_id": flow_id, "menu_choice": "light"},
                )
        finally:
            if flow_id:
                await safe_call_tool(
                    mcp_client,
                    "ha_set_integration",
                    {"domain": "group", "flow_id": flow_id, "abort": True},
                )

        assert menu["flow_type"] == "menu"
        assert "light" in menu["menu_options"]
        assert light["flow_id"] == flow_id
        assert light["flow_type"] == "form"
        assert light["step_id"] == "light"
        fields = {f["name"]: f for f in light["fields"]}
        assert "entities" in fields
        assert fields["all"]["description"] == expected_all

    async def test_single_instance_setup_flow_returns_cores_abort_text(
        self, mcp_client, ha_client
    ):
        """sun is already set up in the test instance, so Core aborts its flow;
        the error carries Core's reason text."""
        async with MCPAssertions(mcp_client) as mcp:
            entries = await mcp.call_tool_success(
                "ha_get_integration", {"domain": "sun"}
            )
            assert entries["entries"], "the test instance must have a sun entry"
            data = await mcp.call_tool_failure(
                "ha_set_integration", {"domain": "sun"}, expected_error="Flow aborted"
            )

        # Core's own abort for the same request names where its text lives.
        core_abort = await ha_client.start_config_flow("sun")
        assert core_abort["type"] == "abort"
        text_domain = core_abort.get("translation_domain") or "sun"
        strings = await _config_strings(ha_client, text_domain)
        expected = strings[
            f"component.{text_domain}.config.abort.{core_abort['reason']}"
        ]

        assert data["details"]["reason"] == core_abort["reason"]
        assert data["reason_text"] == expected

    async def test_an_entry_alone_reads_its_options_form(self, mcp_client):
        """The demo entry's options flow opens with form options_1; the flow
        stays open with its flow_id until aborted."""
        flow_id = aborted = None
        async with MCPAssertions(mcp_client) as mcp:
            listed = await mcp.call_tool_success(
                "ha_get_integration", {"domain": "demo"}
            )
            with_options = [e for e in listed["entries"] if e.get("supports_options")]
            assert with_options, f"no demo entry with options: {listed['entries']}"
            entry = with_options[0]
            try:
                data = await mcp.call_tool_success(
                    "ha_set_integration",
                    {"entry_id": entry["entry_id"]},
                )
                flow_id = data["flow_id"]
            finally:
                if flow_id:
                    aborted = await safe_call_tool(
                        mcp_client,
                        "ha_set_integration",
                        {
                            "entry_id": entry["entry_id"],
                            "flow_id": flow_id,
                            "abort": True,
                        },
                    )

        assert data["flow_type"] == "form"
        assert data["step_id"] == "options_1"
        assert "bool" in {f["name"] for f in data["fields"]}
        assert "entry" not in data
        # Only an open flow can be aborted.
        assert aborted and aborted.get("success") is True, aborted

    async def test_unknown_domain_fails_the_setup_form(self, mcp_client):
        """A domain Core has no config flow for has no setup form to describe."""
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_set_integration",
                {"domain": "no_such_integration_xyz"},
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
                described_plane = await mcp.call_tool_success(
                    "ha_set_integration",
                    {
                        "entry_id": entry_id,
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
        # ha_set_integration(subentry_type=...) reads the same form.
        described = described_plane["subentry_schema"]
        assert (described["flow_type"], described["step_id"]) == ("form", "reconfigure")
        assert [f["name"] for f in described["fields"]] == [
            f["name"] for f in edit_schema["fields"]
        ]
