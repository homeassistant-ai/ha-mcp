"""Run legacy device operations without their state listener: an autouse
fixture for the modules that import it."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import device_control
from ha_mcp.utils.operation_manager import get_operation_manager


@pytest.fixture(autouse=True)
def operations_without_listener(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The legacy ops' state listener is this server's own service; no test
    here reads what it would deliver. Operations start and end empty."""
    monkeypatch.setattr(
        device_control, "start_websocket_listener", AsyncMock(return_value=False)
    )
    get_operation_manager().operations.clear()
    yield
    get_operation_manager().operations.clear()
