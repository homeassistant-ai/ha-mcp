"""The device each HA-MCP config entry is listed under."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import entry_device  # noqa: E402
from custom_components.ha_mcp_tools.const import COMPONENT_VERSION  # noqa: E402


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    registry = SimpleNamespace(async_get_or_create=lambda **kw: calls.append(kw))
    fake_dr = ModuleType("homeassistant.helpers.device_registry")
    fake_dr.async_get = MagicMock(return_value=registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", fake_dr)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "device_registry", fake_dr, raising=False
    )
    return calls


def _manifest(monkeypatch: pytest.MonkeyPatch, read: AsyncMock) -> None:
    monkeypatch.setattr(
        sys.modules["homeassistant.loader"],
        "async_get_integration",
        read,
        raising=False,
    )


async def _register() -> None:
    await entry_device.async_register_entry_device(
        MagicMock(), SimpleNamespace(entry_id="e1"), name="N", model="M"
    )


async def test_the_device_reports_the_installed_release(monkeypatch, created) -> None:
    _manifest(monkeypatch, AsyncMock(return_value=SimpleNamespace(version="9.1.0")))
    await _register()
    assert created[0]["sw_version"] == "9.1.0"


@pytest.mark.parametrize(
    "read",
    [
        AsyncMock(return_value=SimpleNamespace(version=None)),
        AsyncMock(side_effect=OSError("unreadable")),
    ],
    ids=["version-less manifest", "unreadable manifest"],
)
async def test_a_manifest_problem_never_reads_none_or_blocks_setup(
    monkeypatch, created, read
) -> None:
    _manifest(monkeypatch, read)
    await _register()
    assert created[0]["sw_version"] == COMPONENT_VERSION
