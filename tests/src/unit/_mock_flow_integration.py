"""Mocked integrations whose flow strings Core reads from disk, for flow tests.

Core reads a mocked integration's translations from its relative package
path, so the files live under the test's own working directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import websocket_get_translations
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockModule, mock_integration, mock_platform


def answer_translations(hass: HomeAssistant) -> None:
    """Register Core's own ``frontend/get_translations`` command.

    The frontend integration cannot be set up here (its web assets are not
    installed), so only its translations command is registered.
    """
    websocket_api.async_register_command(hass, websocket_get_translations)


def mock_flow_integration(
    hass: HomeAssistant,
    domain: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strings: dict[str, Any] | None = None,
) -> None:
    """A mocked integration with a config flow platform and, given
    ``strings``, the English translations Core reads for it."""
    if strings is not None:
        monkeypatch.chdir(tmp_path)
        folder = tmp_path / "homeassistant" / "components" / domain / "translations"
        folder.mkdir(parents=True)
        (folder / "en.json").write_text(json.dumps(strings), encoding="utf-8")
    mock_integration(
        hass,
        MockModule(domain),
        top_level_files={"translations"} if strings is not None else None,
    )
    mock_platform(hass, f"{domain}.config_flow")


def open_flow_ids(hass: HomeAssistant) -> list[str]:
    """The flows open in Core's config, options and subentry flow managers."""
    managers = (
        hass.config_entries.flow,
        hass.config_entries.options,
        hass.config_entries.subentries,
    )
    return [flow["flow_id"] for m in managers for flow in m.async_progress()]


def options_only_config_flow(options_flow: type[OptionsFlow]) -> type[ConfigFlow]:
    """A config flow class whose entries change through ``options_flow``."""

    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return options_flow()

    return type(
        "OptionsOnlyConfigFlow",
        (ConfigFlow,),
        {"VERSION": 1, "async_get_options_flow": staticmethod(async_get_options_flow)},
    )
