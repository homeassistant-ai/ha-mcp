"""``WebSocketManager.pooled_client``: the pool's connection, read without connecting.

The component capability cache compares it with the connection its caps were
probed over (#2967), on every cached read, so it must never open one.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.client import websocket_client


class _FakeConnection:
    def __init__(self, url: str, token: str, verify_ssl: bool | None = None) -> None:
        self.url = url
        self.is_connected = False

    async def connect(self) -> bool:
        self.is_connected = True
        return True

    async def disconnect(self) -> None:
        self.is_connected = False


@pytest.fixture
def made() -> list[_FakeConnection]:
    return []


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, made: list[_FakeConnection]) -> Any:
    """The process-wide pool, emptied, opening fake connections."""
    pool = websocket_client.websocket_manager

    def factory(*args: Any, **kwargs: Any) -> _FakeConnection:
        made.append(_FakeConnection(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(pool, "_clients", {})
    monkeypatch.setattr(pool, "_last_used", {})
    monkeypatch.setattr(pool, "_client_factory", factory)
    return pool


async def test_returns_the_pooled_connection_without_connecting(
    manager: Any, made: list[_FakeConnection]
) -> None:
    pooled = await manager.get_client(url="http://ha:8123", token="t", verify_ssl=True)

    found = manager.pooled_client(url="http://ha:8123", token="t", verify_ssl=True)

    assert found is pooled
    assert len(made) == 1


async def test_a_dropped_connection_is_returned_until_replaced(manager: Any) -> None:
    pooled = await manager.get_client(url="http://ha:8123", token="t", verify_ssl=True)
    pooled.is_connected = False

    assert manager.pooled_client("http://ha:8123", "t", True) is pooled

    replaced = await manager.get_client(
        url="http://ha:8123", token="t", verify_ssl=True
    )
    assert replaced is not pooled
    assert manager.pooled_client("http://ha:8123", "t", True) is replaced


async def test_other_credentials_have_no_connection(
    manager: Any, made: list[_FakeConnection]
) -> None:
    await manager.get_client(url="http://ha:8123", token="t", verify_ssl=True)

    assert manager.pooled_client("http://ha:8123", "other", True) is None
    assert manager.pooled_client("http://ha:8123", "t", False) is None
    assert len(made) == 1
