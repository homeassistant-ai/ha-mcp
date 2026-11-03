"""Unit tests for the component-side YAML key discovery service."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

# The component imports Home Assistant at module import; unit tests run without it.
sys.modules.setdefault("homeassistant", SimpleNamespace())
for _module in (
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.config",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.storage",
    "homeassistant.loader",
):
    sys.modules.setdefault(_module, MagicMock())

from custom_components.ha_mcp_tools import yaml_find  # noqa: E402

yaml_find.YAML_CONFIG_FILE = "configuration.yaml"


def _hass():
    async def _executor(func, *args):
        return func(*args)

    return SimpleNamespace(
        config=SimpleNamespace(config_dir="/config"),
        async_add_executor_job=_executor,
        services=MagicMock(),
    )


def _call(yaml_path="template"):
    return SimpleNamespace(data={"yaml_path": yaml_path})


def test_handler_returns_relative_files(monkeypatch):
    def fake_load(path, secrets):
        assert path == "/config/configuration.yaml"
        return {"template": {}}

    monkeypatch.setattr(yaml_find, "load_yaml_config_file", fake_load)
    monkeypatch.setattr(
        yaml_find,
        "find_yaml_key_files",
        lambda config, path: ["/config/packages/a.yaml", "/outside/b.yaml"],
    )
    handler = yaml_find.build_find_yaml_key_handler(
        _hass(),
        caller_token_ok=lambda hass, call: True,
        unauthorized_response=lambda service: {"success": False},
    )

    result = asyncio.run(handler(_call()))

    assert result == {"success": True, "files": ["packages/a.yaml"]}


def test_handler_rejects_bad_token():
    handler = yaml_find.build_find_yaml_key_handler(
        _hass(),
        caller_token_ok=lambda hass, call: False,
        unauthorized_response=lambda service: {"success": False, "service": service},
    )

    result = asyncio.run(handler(_call()))

    assert result == {"success": False, "service": "find_yaml_key"}


def test_handler_reports_config_load_failure(monkeypatch):
    def fake_load(path, secrets):
        raise RuntimeError("bad config")

    monkeypatch.setattr(yaml_find, "load_yaml_config_file", fake_load)
    handler = yaml_find.build_find_yaml_key_handler(
        _hass(),
        caller_token_ok=lambda hass, call: True,
        unauthorized_response=lambda service: {"success": False},
    )

    result = asyncio.run(handler(_call()))

    assert result == {"success": False, "error": "bad config", "files": []}


def test_register_find_yaml_key_uses_response_schema():
    hass = _hass()
    yaml_find.register_find_yaml_key(
        hass,
        caller_token_ok=lambda hass, call: True,
        unauthorized_response=lambda service: {"success": False},
    )

    args, kwargs = hass.services.async_register.call_args
    assert args[:2] == ("ha_mcp_tools", "find_yaml_key")
    assert kwargs["schema"] is yaml_find.SERVICE_FIND_YAML_KEY_SCHEMA
