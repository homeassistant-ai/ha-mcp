"""Start ``ha-mcp-web`` (``main_web``) in a unit test, up to where it would serve."""

from __future__ import annotations

import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import FastMCP


@dataclass
class WebStart:
    """What ``main_web`` set up before handing its server to the runner."""

    mcp: FastMCP
    serve: Coroutine[Any, Any, Any]


def start_main_web(
    monkeypatch: pytest.MonkeyPatch, *, keep_serve: bool = False, **env: str | None
) -> WebStart:
    """Run ``main_web`` with ``env`` set; a ``None`` value unsets the variable.

    ``main_web`` builds the real server; the runner keeps the coroutine it is
    handed instead of running it: ``serve`` is closed unless ``keep_serve``,
    for a test that awaits it. Logging is left as pytest set it up, so
    ``caplog`` sees the startup lines.
    """
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://homeassistant.invalid:8123")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "test-token")
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    handed: list[Coroutine[Any, Any, Any]] = []
    monkeypatch.setattr("ha_mcp.__main__.setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(
        "ha_mcp.__main__.run_entrypoint", lambda coro, label: handed.append(coro)
    )
    # Process-wide state a startup leaves behind, put back after the test.
    access_log = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access_log, "filters", list(access_log.filters))
    from ha_mcp.__main__ import get_mcp, main_web, reset_main_runtime
    from ha_mcp.config import reset_global_settings
    from ha_mcp.settings_ui import reset_http_settings_state

    reset_main_runtime()
    reset_http_settings_state()
    reset_global_settings()

    main_web()
    [serve] = handed
    if not keep_serve:
        serve.close()
    return WebStart(get_mcp(), serve)
