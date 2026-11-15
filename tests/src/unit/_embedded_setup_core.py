"""The server entry's bring-up on a real ``hass``, with the server and webhook replaced.

``Collaborators`` replaces this component's own collaborators (the in-process
server's start and stop, the webhook and LLM API registration); ``Notices`` records
the persistent notifications Core's own service raises and dismisses.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import (
    Notification,
    UpdateType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.core_config import async_process_ha_core_config

from custom_components.ha_mcp_tools import embedded_setup as esetup

SERVER_MODULE = "ha_mcp.tools.tools_filesystem"


class Collaborators:
    """The replaced collaborators a bring-up test asserts on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.register_webhook = AsyncMock(return_value=False)
        self.register_llm_api = AsyncMock()
        self.legacy_active = MagicMock(return_value=True)
        replaced: dict[tuple[object, str], object] = {
            (esetup.EmbeddedServerManager, "async_start"): self.start,
            (esetup.EmbeddedServerManager, "async_stop"): self.stop,
            (esetup, "async_register_webhook"): self.register_webhook,
            (esetup, "async_register_llm_api"): self.register_llm_api,
            (esetup, "legacy_credentials_active"): self.legacy_active,
        }
        for (target, name), spy in replaced.items():
            monkeypatch.setattr(target, name, spy)
        server_not_installed(monkeypatch)

    @property
    def webhook_kwargs(self) -> Mapping[str, Any]:
        """The keyword arguments the webhook was last registered with."""
        call = self.register_webhook.await_args
        assert call is not None
        return call.kwargs

    @property
    def llm_api_kwargs(self) -> Mapping[str, Any]:
        """The keyword arguments the LLM API was last registered with."""
        call = self.register_llm_api.await_args
        assert call is not None
        return call.kwargs


def server_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ha-mcp package is not importable, so no component minimum is read."""
    monkeypatch.setitem(sys.modules, SERVER_MODULE, None)


def server_requires_component(
    monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    """The installed server declares ``version`` as its minimum component, or
    ``None`` for an older server layout that declares no minimum."""
    module = ModuleType(SERVER_MODULE)
    if version is not None:
        module.__dict__["MIN_COMPONENT_VERSION"] = version
    monkeypatch.setitem(sys.modules, SERVER_MODULE, module)


def nabu_casa_url(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    """Answer the cloud integration's remote URL (its requirements are not
    installed with Core, so the component's own lookup is replaced)."""

    def remote_url(hass: HomeAssistant) -> str | None:
        return url

    monkeypatch.setattr(esetup, "_cloud_remote_url", remote_url)


async def set_internal_url(hass: HomeAssistant, url: str) -> None:
    """Configure Home Assistant's internal URL through Core's own config."""
    await async_process_ha_core_config(hass, {"internal_url": url})


class Notices:
    """The persistent notifications Core's service raised or dismissed."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.added: list[Notification] = []
        self.removed: list[str] = []
        self.stop = persistent_notification.async_register_callback(hass, self._record)

    @callback
    def _record(
        self, update_type: UpdateType, notifications: dict[str, Notification]
    ) -> None:
        if update_type in (UpdateType.ADDED, UpdateType.UPDATED):
            self.added.extend(notifications.values())
        elif update_type is UpdateType.REMOVED:
            self.removed.extend(notifications)

    @property
    def message(self) -> str:
        """The message of the one notification that was raised."""
        assert len(self.added) == 1, self.added
        return self.added[0]["message"]
