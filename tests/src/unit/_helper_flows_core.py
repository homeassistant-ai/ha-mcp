"""Core's helper flow list and a custom helper integration, for the helper flow tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.components.config import config_entries as config_entries_view
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import (
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ._core_setup import install_custom_helper

CUSTOM = "my_custom_helper"


@contextmanager
def flow_list_reads() -> Iterator[AsyncMock]:
    """Count Core's reads of the helper flow list; Core still answers each one."""
    with patch.object(
        config_entries_view,
        "async_get_config_flows",
        wraps=config_entries_view.async_get_config_flows,
    ) as spy:
        yield spy


class MonotonicClock:
    """The reading of ``time.monotonic`` the helper flow cache sees."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _CustomHelperFlow(ConfigFlow):
    """A helper integration's config flow: a name and a source entity."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=probatio.Schema(
                    {probatio.Required("name"): str, probatio.Required("source"): str}
                ),
            )
        return self.async_create_entry(title=user_input["name"], data=user_input)


@pytest.fixture
def custom_helper_flow(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enable_custom_integrations: None,
) -> Iterator[None]:
    """``my_custom_helper``: a custom helper integration Core's loader finds on
    disk and whose config flow runs."""
    install_custom_helper(tmp_path, monkeypatch, CUSTOM)
    mock_integration(hass, MockModule(CUSTOM))
    mock_platform(hass, f"{CUSTOM}.config_flow")
    with mock_config_flow(CUSTOM, _CustomHelperFlow):
        yield
