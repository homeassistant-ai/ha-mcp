"""Locate config files that define a dotted YAML key.

Kept out of ``__init__.py`` because that module is over the line ratchet. The
handler reads Core's loaded config with the annotations its loader attaches, so
every source file Core loaded is discoverable without a one-level glob. It uses
``load_yaml_config_file`` rather than the package-merge path: merging validates
packages, logs their errors, and drops rejected ones from the result.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import voluptuous as vol
from annotatedyaml import Secrets
from homeassistant.config import YAML_CONFIG_FILE, load_yaml_config_file
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service as ha_service

from .const import DOMAIN
from .yaml_rt import find_yaml_key_files

SERVICE_FIND_YAML_KEY = "find_yaml_key"
SERVICE_FIND_YAML_KEY_SCHEMA = vol.Schema(
    {
        vol.Required("yaml_path"): cv.string,
        vol.Required("_ha_mcp_token"): cv.string,
    }
)


def _relative_key_files(files: list[str], config_dir: Path) -> list[str]:
    """Map Core's absolute source paths to config-relative readable paths."""
    return sorted(
        {
            os.path.relpath(path, config_dir)
            for path in files
            if os.path.commonpath((config_dir, path)) == str(config_dir)
        }
    )


def _find_relative_key_files(
    config: Any, yaml_path: str, config_dir: Path
) -> list[str]:
    """Walk the loaded config off the event loop and map its paths."""
    return _relative_key_files(find_yaml_key_files(config, yaml_path), config_dir)


def build_find_yaml_key_handler(
    hass: HomeAssistant,
    *,
    caller_token_ok: Callable[[HomeAssistant, ServiceCall], bool],
    unauthorized_response: Callable[[str], dict[str, Any]],
) -> Callable[[ServiceCall], Coroutine[Any, Any, dict[str, Any]]]:
    """Build the handler that locates a key in Core's loaded YAML config."""
    config_dir = Path(hass.config.config_dir)

    async def handle_find_yaml_key(call: ServiceCall) -> dict[str, Any]:
        if not caller_token_ok(hass, call):
            return unauthorized_response(SERVICE_FIND_YAML_KEY)
        yaml_path = call.data["yaml_path"]
        try:
            config = await hass.async_add_executor_job(
                load_yaml_config_file,
                str(config_dir / YAML_CONFIG_FILE),
                Secrets(config_dir),
            )
            files = await hass.async_add_executor_job(
                _find_relative_key_files, config, yaml_path, config_dir
            )
        except Exception as err:  # noqa: BLE001
            return {"success": False, "error": str(err), "files": []}
        return {"success": True, "files": files}

    return handle_find_yaml_key


def register_find_yaml_key(
    hass: HomeAssistant,
    caller_token_ok: Callable[[HomeAssistant, ServiceCall], bool],
    unauthorized_response: Callable[[str], dict[str, Any]],
) -> None:
    """Register the read-only key finder on the tools entry."""
    ha_service.async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_FIND_YAML_KEY,
        build_find_yaml_key_handler(
            hass,
            caller_token_ok=caller_token_ok,
            unauthorized_response=unauthorized_response,
        ),
        schema=SERVICE_FIND_YAML_KEY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
