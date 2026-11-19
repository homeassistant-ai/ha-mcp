"""Full-page fallback is limited to Supervisor-verified legacy Puppet."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.dashboard_screenshot import capture

from ._dashboard_screenshot_common import PNG, _puppet_info, set_settings
from ._screenshot_engine import Engine, Render, resolve_puppet_hostname
from ._screenshot_engine import engine as engine
from ._tools_addons_core import Supervisor, installed_apps
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app


def _installed_puppet(
    monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor, engine: Engine, version: str
) -> None:
    info = _puppet_info(state="started", hostname="local-puppet", version=version)
    # This engine has no Home Assistant account used for theme restoration.
    info["options"]["access_token"] = ""
    supervisor.answer("GET", "/addons", installed_apps("local_puppet"))
    supervisor.answer("GET", "/addons/local_puppet/info", info)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "app-token")
    monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor.url}")
    set_settings(monkeypatch, dashboard_screenshot_engine_url="")
    port = urlsplit(engine.url).port
    assert port is not None
    resolve_puppet_hostname(monkeypatch, port)


async def test_verified_legacy_puppet_retries_with_maximum_height(
    monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor, engine: Engine
) -> None:
    _installed_puppet(monkeypatch, supervisor, engine, "2.4.9")
    engine.next_response = [Render(400, b"", content_type="text/plain"), Render(200, PNG)]

    result = await capture.capture_dashboard_images("lovelace/0", width=900, full_page=True)

    assert [request["params"]["viewport"] for request in engine.gets] == ["900xauto", "900x4096"]
    assert result[0].height == 4096
    assert result[0].legacy_full_page_fallback is True


@pytest.mark.parametrize("version", [None, "2.5.0"], ids=["explicit-url", "current-puppet"])
async def test_only_verified_legacy_puppet_retries_a_rejected_viewport(
    monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor, engine: Engine, version: str | None
) -> None:
    if version is not None:
        _installed_puppet(monkeypatch, supervisor, engine, version)
    engine.next_response = Render(400, b"invalid viewport", content_type="text/plain")

    with pytest.raises(ToolError) as exc_info:
        await capture.capture_dashboard_images("lovelace/0", width=900, full_page=True)

    [request] = engine.gets
    assert request["path"] == "/lovelace/0"
    assert request["params"]["viewport"] == "900xauto"
    error = json.loads(str(exc_info.value))
    assert error["engine_kind"] == ("puppet" if version else "unknown")
    if version:
        assert error["engine_version"] == version
