"""The dev-env shim prints a tool result the way the client received it."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from ha_mcp._vendor import mcp_types
from ha_mcp._vendor.fastmcp import Client, FastMCP

_SHIM = Path(__file__).resolve().parents[3] / ".github" / "dev-ha-env" / "mcpshim.py"
_PNG = b"\x89PNG\r\n\x1a\nshim"


def _load_shim() -> Any:
    spec = importlib.util.spec_from_file_location("mcpshim", _SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def call_screenshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Drive the CLI through a real MCP client and an in-memory MCP server."""
    server = FastMCP("shim test")

    @server.tool
    def ha_get_dashboard_screenshot() -> mcp_types.CallToolResult:
        return mcp_types.CallToolResult.model_validate(
            {
                "content": [
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": base64.b64encode(_PNG).decode(),
                    }
                ],
                "structuredContent": {"warnings": ["engine changed the theme"]},
                "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "ha-mcp"}},
            }
        )

    shim = _load_shim()
    monkeypatch.setattr(shim, "Client", lambda _url, **kwargs: Client(server, **kwargs))
    urls = tmp_path / "urls.txt"
    urls.write_text("MCP: http://127.0.0.1:9583/mcp\n", encoding="utf-8")

    def call():
        shim.main(["mcpshim.py", str(urls), "call", "ha_get_dashboard_screenshot"])

    return call


def test_an_image_result_keeps_its_structured_content_and_meta(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    call_screenshot,
) -> None:
    """A screenshot's warnings live only in structuredContent beside the image."""
    monkeypatch.delenv("MCPSHIM_SAVE_MEDIA", raising=False)
    call_screenshot()

    printed = json.loads(capsys.readouterr().out)
    assert printed["structuredContent"] == {"warnings": ["engine changed the theme"]}
    assert printed["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "ha-mcp"
    assert hashlib.sha256(_PNG).hexdigest() in printed["content"][0]["data"]
    assert base64.b64encode(_PNG).decode() not in json.dumps(printed)


def test_media_saves_into_a_directory_that_does_not_exist_yet(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    call_screenshot,
) -> None:
    """A missing save directory must not lose the result of a call that ran."""
    save_dir = tmp_path / "new" / "media"
    monkeypatch.setenv("MCPSHIM_SAVE_MEDIA", str(save_dir))
    call_screenshot()

    saved = Path(json.loads(capsys.readouterr().out)["content"][0]["saved_to"])
    assert saved.parent == save_dir
    assert saved.read_bytes() == _PNG
