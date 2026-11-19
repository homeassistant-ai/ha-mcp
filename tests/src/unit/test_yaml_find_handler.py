"""The YAML discovery service reads Core's on-disk configuration."""

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools.const import DOMAIN

from ._component_filesystem import put
from ._file_services import FileService, make_file_service


@pytest.fixture
async def find_files(hass: HomeAssistant) -> FileService:
    return await make_file_service(hass)


async def test_handler_returns_relative_files(
    hass: HomeAssistant,
    find_files: FileService,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    sibling = tmp_path_factory.mktemp("other-source") / "external.yaml"
    await hass.async_add_executor_job(sibling.write_text, "template: {}\n")
    await put(
        hass,
        {
            "configuration.yaml": (
                "inside: !include packages/a.yaml\n"
                f"outside: !include {sibling.as_posix()}\n"
            ),
            "packages/a.yaml": "template: {}\n",
        },
    )
    result = await find_files("find_yaml_key", {"yaml_path": "template"})
    assert result == {"success": True, "files": [str(Path("packages/a.yaml"))]}


async def test_handler_rejects_bad_token(
    hass: HomeAssistant, find_files: FileService
) -> None:
    result = await hass.services.async_call(
        DOMAIN,
        "find_yaml_key",
        {"yaml_path": "template", "_ha_mcp_token": "invalid"},
        blocking=True,
        return_response=True,
    )
    assert result is not None
    assert result["success"] is False
    assert result["error_code"] == "unauthorized"


async def test_handler_reports_config_load_failure(
    hass: HomeAssistant, find_files: FileService
) -> None:
    await put(hass, {"configuration.yaml": "template: !include missing.yaml\n"})
    result = await find_files("find_yaml_key", {"yaml_path": "template"})
    assert result["success"] is False
    assert result["files"] == []
    assert "missing.yaml" in result["error"]
