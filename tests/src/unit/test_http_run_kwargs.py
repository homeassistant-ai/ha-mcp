"""Unit tests for _http_run_kwargs.

Regression coverage for #1544 — passing stateless_http=True alongside
transport="sse" causes fastmcp.run_async to return immediately without
binding, which the calling _run_entrypoint surfaces as a silent exit
with code 0.
"""

from ha_mcp.__main__ import _http_run_kwargs


def test_http_transport_includes_stateless_http():
    kw = _http_run_kwargs("http", "127.0.0.1", 8086, "/mcp")
    assert kw["stateless_http"] is True


def test_streamable_http_transport_includes_stateless_http():
    kw = _http_run_kwargs("streamable-http", "127.0.0.1", 8086, "/mcp")
    assert kw["stateless_http"] is True


def test_sse_transport_omits_stateless_http():
    """Regression #1544: stateless_http=True + transport=sse silently no-ops.

    The previous _http_run_kwargs unconditionally included
    stateless_http=True. When paired with transport="sse", fastmcp's
    run_async returned immediately without binding, and ha-mcp's
    _run_entrypoint then sys.exit(0)'d because the coroutine completed
    without raising — producing a silent exit with success code that
    hid the misconfiguration.
    """
    kw = _http_run_kwargs("sse", "127.0.0.1", 8087, "/sse")
    assert "stateless_http" not in kw


def test_common_kwargs_present_across_transports():
    """Non-stateless kwargs are identical regardless of transport."""
    common_keys = {"transport", "host", "port", "path", "show_banner", "uvicorn_config"}
    for transport in ("http", "sse", "streamable-http"):
        kw = _http_run_kwargs(transport, "127.0.0.1", 8086, "/p")
        assert common_keys.issubset(kw.keys()), f"missing keys for transport={transport}"
        assert kw["transport"] == transport
        assert kw["host"] == "127.0.0.1"
        assert kw["port"] == 8086
        assert kw["path"] == "/p"
