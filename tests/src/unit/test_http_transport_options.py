"""Opt-in HTTP experiments preserve normal FastMCP responses when disabled."""

import asyncio
import json
import logging

import fastmcp
import pytest
from starlette.requests import ClientDisconnect, Request
from starlette.testclient import TestClient

from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.http_transport import HttpTransportFastMCP, TransportDiagnostics
from ha_mcp.utils.data_paths import get_data_dir


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://localhost:8123")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "test-token")
    for name in (
        "HAMCP_HTTP_TRANSPORT_DIAGNOSTICS",
        "HAMCP_HTTP_JSON_RESPONSE",
        "FASTMCP_JSON_RESPONSE",
        "SUPERVISOR_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fastmcp.settings, "json_response", False)
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", False)
    get_data_dir.cache_clear()
    reset_global_settings()
    yield
    reset_global_settings()
    get_data_dir.cache_clear()


@pytest.mark.parametrize("diagnostics", [False, True])
@pytest.mark.parametrize("json_response", [False, True])
def test_real_http_tool_response(monkeypatch, caplog, diagnostics, json_response):
    """Both independent settings work through the existing embedded call shape."""
    monkeypatch.setenv("HAMCP_HTTP_TRANSPORT_DIAGNOSTICS", str(diagnostics))
    monkeypatch.setenv("HAMCP_HTTP_JSON_RESPONSE", str(json_response))
    caplog.set_level(logging.INFO, logger="ha_mcp.http_transport")
    mcp = HttpTransportFastMCP("test")

    @mcp.tool
    def echo(value: str) -> str:
        return value

    app = mcp.http_app(path="/private-secret", stateless_http=True)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/private-secret",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 17,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"value": "payload-secret"}},
            },
        )
    assert response.status_code == 200
    if json_response:
        assert response.headers["content-type"].startswith("application/json")
        result = response.json()
    else:
        assert response.headers["content-type"].startswith("text/event-stream")
        messages = (
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        )
        result = next(message for message in messages if message.get("id") == 17)
    assert result["id"] == 17
    assert result["result"]["content"][0]["text"] == "payload-secret"
    records = [r for r in caplog.records if r.name == "ha_mcp.http_transport"]
    assert bool(records) is diagnostics
    if diagnostics:
        assert "request_complete=True" in caplog.text
        assert "response_complete=True" in caplog.text
        assert "private-secret" not in "\n".join(r.getMessage() for r in records)
        assert "payload-secret" not in "\n".join(r.getMessage() for r in records)


@pytest.mark.parametrize("diagnostics", [False, True])
def test_existing_fastmcp_defaults_and_positional_arguments(
    monkeypatch, caplog, diagnostics
):
    """Preserve FastMCP JSON defaults and caller middleware in both modes."""
    monkeypatch.setenv("HAMCP_HTTP_TRANSPORT_DIAGNOSTICS", str(diagnostics))
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    monkeypatch.setattr(fastmcp.settings, "json_response", True)
    caplog.set_level(logging.INFO, logger="ha_mcp.http_transport")
    middleware = [Middleware(CORSMiddleware, allow_origins=["https://example.com"])]
    app = HttpTransportFastMCP("test").http_app("/mcp", middleware, stateless_http=True)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Origin": "https://example.com",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["access-control-allow-origin"] == "https://example.com"
    assert response.json()["id"] == 1
    assert len(middleware) == 1
    assert any(r.name == "ha_mcp.http_transport" for r in caplog.records) is diagnostics


async def test_settings_ui_persists_independent_toggles(tmp_path):
    from ha_mcp.settings_ui._handlers_advanced import build_advanced_handlers

    handlers = build_advanced_handlers(None)
    changes = {"http_transport_diagnostics": True, "http_json_response": False}

    async def receive():
        return {"type": "http.request", "body": json.dumps(changes).encode()}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    response = await handlers["save_advanced_settings"](request)
    assert response.status_code == 200
    assert json.loads(response.body)["restart_required"] is True
    reset_global_settings()
    settings = get_global_settings()
    assert settings.http_transport_diagnostics is True
    assert settings.http_json_response is False
    assert json.loads((tmp_path / "feature_flags.json").read_text()) == changes


async def test_diagnostics_observe_without_changing_messages(caplog):
    """Counters observe streaming chunks; they neither buffer nor rewrite them."""
    caplog.set_level(logging.INFO, logger="ha_mcp.http_transport")
    incoming = [
        {"type": "http.request", "body": b"sec", "more_body": True},
        {"type": "http.request", "body": b"ret", "more_body": False},
    ]
    outgoing = [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"result", "more_body": True},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]
    seen = []
    sent = []

    async def receive():
        return incoming[len(seen)]

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())
        for message in outgoing:
            await send(message)

    scope = {"type": "http", "path": "/secret", "headers": [(b"content-length", b"6")]}
    await TransportDiagnostics(app, path="/secret")(scope, receive, send)
    assert all(a is b for a, b in zip(seen, incoming, strict=True))
    assert all(a is b for a, b in zip(sent, outgoing, strict=True))
    assert "request_bytes=6" in caplog.text
    assert "response_bytes=6" in caplog.text
    assert "declared_bytes=6" in caplog.text
    assert "response_complete=True" in caplog.text
    assert "response body complete" in caplog.text
    assert "secret" not in caplog.text


async def test_incomplete_upload_and_disconnect(caplog):
    caplog.set_level(logging.INFO, logger="ha_mcp.http_transport")
    incoming = iter(
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(incoming)

    async def send(message):
        pytest.fail("Incomplete upload must not create a response")

    async def app(scope, receive, send):
        request = Request(scope, receive)
        await request.body()

    with pytest.raises(ClientDisconnect):
        await TransportDiagnostics(app, path="/mcp")(
            {"type": "http", "path": "/mcp", "headers": [(b"content-length", b"100")]},
            receive,
            send,
        )
    assert "request_bytes=3" in caplog.text
    assert "declared_bytes=100" in caplog.text
    assert "request_complete=False" in caplog.text
    assert "disconnect=True" in caplog.text


async def test_failed_send_is_not_reported_as_complete(caplog):
    caplog.set_level(logging.INFO, logger="ha_mcp.http_transport")

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {"type": "http.response.body", "body": b"secret", "more_body": False}
        )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("secret path or credential")

    with pytest.raises(OSError):
        await TransportDiagnostics(app, path="/mcp")(
            {"type": "http", "path": "/mcp", "headers": []}, receive, send
        )
    assert "response_complete=False" in caplog.text
    assert "response_bytes=0" in caplog.text
    assert "error=OSError" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("error", [None, asyncio.CancelledError("secret")])
async def test_unfinished_response_and_cancellation(caplog, error):
    caplog.set_level(logging.INFO, logger="ha_mcp.http_transport")

    async def app(scope, receive, send):
        await send({"type": "http.response.body", "body": b"abc", "more_body": True})
        if error is not None:
            raise error

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    operation = TransportDiagnostics(app, path="/mcp")(
        {"type": "http", "path": "/mcp", "headers": []}, receive, send
    )
    if error is None:
        await operation
    else:
        with pytest.raises(asyncio.CancelledError) as raised:
            await operation
        assert raised.value is error
        assert "error=CancelledError" in caplog.text
    assert "response_bytes=3" in caplog.text
    assert "response_complete=False" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    "scope", [{"type": "lifespan"}, {"type": "http", "path": "/settings"}]
)
async def test_other_routes_and_lifespan_are_untouched(caplog, scope):
    caplog.set_level(logging.INFO, logger="ha_mcp.http_transport")

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    async def app(actual_scope, actual_receive, actual_send):
        assert actual_scope is scope
        assert actual_receive is receive
        assert actual_send is send

    await TransportDiagnostics(app, path="/mcp")(scope, receive, send)
    assert not caplog.records
