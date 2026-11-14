"""``ha_get_system_health``'s config_check returns Core's reply as Core sends it.

Core checks a ``configuration.yaml`` the test writes into its own config
directory, so every reply, refusal included, is Core's own.
"""

from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_system import SystemTools


async def _config_check(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    tmp_path: Path,
    configuration_yaml: str | None,
) -> dict[str, Any]:
    """The tool's config_check for ``configuration_yaml`` (``None``: no file)."""
    hass.config.config_dir = str(tmp_path)
    if configuration_yaml is not None:
        (tmp_path / "configuration.yaml").write_text(configuration_yaml)
    assert await async_setup_component(hass, "system_health", {})
    assert await async_setup_component(hass, "config", {})
    result = await SystemTools(client).ha_get_system_health(include="config_check")
    return result["config_check"]


async def test_a_valid_config_comes_back_as_core_sends_it(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    check = await _config_check(hass, core_client, tmp_path, "homeassistant: {}\n")

    assert check == {"result": "valid", "errors": None, "warnings": None}


async def test_a_missing_configuration_yaml_is_invalid(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    check = await _config_check(hass, core_client, tmp_path, None)

    assert check["result"] == "invalid"
    assert isinstance(check["errors"], str)


async def test_warnings_alone_leave_the_config_valid(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    configuration = "homeassistant: {}\nno_such_integration_probe:\n"

    check = await _config_check(hass, core_client, tmp_path, configuration)

    assert check["result"] == "valid"
    assert "no_such_integration_probe" in check["warnings"]


async def test_cores_refusal_is_reported_in_the_section(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_read_only_access_token: str,
    tmp_path: Path,
) -> None:
    client = HomeAssistantClient(
        base_url=core_client.base_url,
        token=hass_read_only_access_token,
        verify_ssl=False,
    )
    try:
        check = await _config_check(hass, client, tmp_path, "homeassistant: {}\n")
    finally:
        await client.close()

    assert check["error"]
