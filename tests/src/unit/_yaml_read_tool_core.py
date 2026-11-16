"""ha_config_get_yaml on real Home Assistant Core.

The files sit in Core's config directory, and every read, listing and key
lookup is answered by the File & YAML Tools entry's own services.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_yaml_read import YamlReadTools

from ._component_filesystem import put
from ._file_services import make_file_service

GetYaml = Callable[..., Awaitable[dict[str, Any]]]

# Core loads it and it includes no package folder, so Core credits no package
# file with a key and a glob is answered by listing the folder.
CONFIGURATION = "homeassistant:\n  name: Test home\n"
# Not YAML: the entry reports it as a parse error at this position.
NOT_YAML = "rest: [\n"
NOT_YAML_ERROR = "not valid YAML at line 2, column 1"
# A date with no 13th month: the entry's parser fails on it outside its YAML
# error handling, so Core answers the read with a server error.
FAILS_TO_READ = "rest:\n  since: 2023-13-01\n"


async def yaml_tool(hass: HomeAssistant, client: HomeAssistantClient) -> GetYaml:
    """The tool, on a ``hass`` with the tools entry and ``CONFIGURATION``."""
    await make_file_service(hass)
    await put(hass, {"configuration.yaml": CONFIGURATION})
    return YamlReadTools(client).ha_config_get_yaml


def found(result: dict[str, Any]) -> list[str]:
    return [match["file"] for match in result["matches"]]


def read_failed(target: str, warning: str) -> bool:
    """True when ``warning`` reports that the read of ``target`` failed."""
    return warning.startswith(f"{target} was not searched: API error: 500")
