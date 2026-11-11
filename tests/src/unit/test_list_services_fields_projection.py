"""Unit tests for fields= projection in ha_list_services (issue #1199).

The tool runs on the server's own client against a real ``hass`` that answers
the component's service catalog command; two light services are registered.
"""

from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.service import async_set_service_schema

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_services import ServiceDiscoveryTools


@pytest.fixture
async def list_services_tool(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> Any:
    """``ha_list_services`` on a ``hass`` with light.turn_on and light.turn_off.

    Each service is described the way an integration describes a service it
    defines at runtime, so Core reads no services.yaml for it.
    """

    async def _noop(call: ServiceCall) -> None:
        return None

    for service, description in (
        ("turn_on", "Turn on a light"),
        ("turn_off", "Turn off a light"),
    ):
        hass.services.async_register("light", service, _noop)
        async_set_service_schema(
            hass, "light", service, {"description": description, "fields": {}}
        )
    return ServiceDiscoveryTools(component_core_client).ha_list_services


class TestHaListServicesFieldsProjection:
    """Tool-level tests for fields= projection in ha_list_services."""

    @pytest.mark.asyncio
    async def test_fields_none_returns_full_response(self, list_services_tool):
        result = await list_services_tool()
        assert "success" in result
        assert "services" in result

    @pytest.mark.asyncio
    async def test_fields_single_key_projects_correctly(self, list_services_tool):
        result = await list_services_tool(fields=["services"])
        assert "services" in result
        assert "success" in result
        assert "domains" not in result

    @pytest.mark.asyncio
    async def test_fields_success_always_retained(self, list_services_tool):
        result = await list_services_tool(fields=["services"])
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_malformed_fields_raises_tool_error(self, list_services_tool):
        with pytest.raises(ToolError):
            await list_services_tool(fields=123)

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, list_services_tool):
        with pytest.raises(ToolError):
            await list_services_tool(fields='["')


class TestHaListServicesServiceFieldsProjection:
    """Unit tests for service_fields= per-record projection in ha_list_services.

    service_fields= projects each service record (name, description, domain,
    service, ...) down to the requested keys.  The typo-guard fires when
    all projected records are empty dicts.
    """

    @pytest.mark.asyncio
    async def test_service_fields_projects_each_record(self, list_services_tool):
        """service_fields=['name'] returns records with only that key."""
        result = await list_services_tool(service_fields=["name"])
        for svc in result["services"].values():
            assert set(svc.keys()) == {"name"}

    @pytest.mark.asyncio
    async def test_service_fields_multiple_keys(self, list_services_tool):
        """service_fields=['name','domain'] returns both keys per record."""
        result = await list_services_tool(service_fields=["name", "domain"])
        for svc in result["services"].values():
            assert set(svc.keys()) == {"name", "domain"}

    @pytest.mark.asyncio
    async def test_service_fields_unknown_key_emits_warning(self, list_services_tool):
        """service_fields with only unknown keys emits a diagnostic in warnings[].

        Pins the typo-footgun guard: service_fields=["nmae"] (typo for "name")
        silently produced {k: {} for k in services} before this fix.
        """
        result = await list_services_tool(service_fields=["nmae"])
        # Every projected service record is empty
        for svc in result["services"].values():
            assert svc == {}
        # Diagnostic warning present and names the wrong field
        assert "warnings" in result, (
            "Expected warnings key when all projected service records are empty"
        )
        assert any("nmae" in w for w in result["warnings"]), (
            f"Expected typo field name in warning, got: {result['warnings']}"
        )

    @pytest.mark.asyncio
    async def test_service_fields_does_not_affect_outer_response_keys(
        self, list_services_tool
    ):
        """service_fields only projects inside services{}; top-level keys unchanged."""
        result = await list_services_tool(service_fields=["name"])
        assert "success" in result
        assert "services" in result
