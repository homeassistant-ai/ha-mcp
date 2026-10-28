"""Per-request MCP client description: protocol version and declared capabilities."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp import mcp_client_context
from ha_mcp._vendor.mcp_types import (
    ClientCapabilities,
    ClientTasksCapability,
    ElicitationCapability,
    FormElicitationCapability,
    SamplingCapability,
    UrlElicitationCapability,
)
from ha_mcp.mcp_client_context import (
    McpClientDebugLogMiddleware,
    capability_names,
    declares_form_elicitation,
    describe_mcp_client,
    extract_client_info,
)


def _ctx(version: str | None, caps: ClientCapabilities | None) -> SimpleNamespace:
    session = SimpleNamespace(client_params=None, client_capabilities=caps)
    return SimpleNamespace(
        session=session,
        request_context=SimpleNamespace(protocol_version=version, session=session),
    )


@pytest.mark.parametrize(
    ("elicitation", "expected"),
    [
        (None, False),
        (ElicitationCapability(), True),
        (ElicitationCapability(form=FormElicitationCapability()), True),
        (ElicitationCapability(url=UrlElicitationCapability()), False),
        (
            ElicitationCapability(
                form=FormElicitationCapability(), url=UrlElicitationCapability()
            ),
            True,
        ),
    ],
)
def test_form_elicitation_includes_the_pre_split_empty_object(
    elicitation: ElicitationCapability | None, expected: bool
) -> None:
    caps = ClientCapabilities(elicitation=elicitation)
    assert declares_form_elicitation(caps) is expected


def test_capability_names_are_flat_and_stable() -> None:
    caps = ClientCapabilities(
        elicitation=ElicitationCapability(
            form=FormElicitationCapability(), url=UrlElicitationCapability()
        ),
        sampling=SamplingCapability(),
        tasks=ClientTasksCapability(),
        extensions={"io.example/b": {}, "io.example/a": {}},
        experimental={"openai/visibility": {}},
    )
    assert capability_names(caps) == [
        "elicitation.form",
        "elicitation.url",
        "sampling",
        "tasks",
        "extension:io.example/a",
        "extension:io.example/b",
        "experimental:openai/visibility",
    ]
    assert capability_names(None) is None
    assert capability_names(ClientCapabilities()) == []


def test_describe_without_a_request_reports_unknown_not_none_declared() -> None:
    assert describe_mcp_client(None) == {
        "protocol_version": "unknown",
        "client_info": {},
        "capabilities": None,
    }


def test_describe_reads_the_request() -> None:
    ctx = _ctx("2026-07-28", ClientCapabilities())
    assert describe_mcp_client(ctx) == {
        "protocol_version": "2026-07-28",
        "client_info": {},
        "capabilities": [],
    }


@pytest.mark.anyio
async def test_debug_log_line_per_tool_call(caplog: pytest.LogCaptureFixture) -> None:
    middleware = McpClientDebugLogMiddleware()
    context = SimpleNamespace(
        message=SimpleNamespace(name="ha_get_state"),
        fastmcp_context=_ctx(
            "2025-11-25", ClientCapabilities(elicitation=ElicitationCapability())
        ),
    )
    call_next = AsyncMock(return_value="ok")
    with caplog.at_level(logging.DEBUG, logger="ha_mcp.mcp_client_context"):
        assert await middleware.on_call_tool(context, call_next) == "ok"
    assert "tools/call ha_get_state" in caplog.text
    assert "2025-11-25" in caplog.text
    assert "elicitation.form" in caplog.text


@pytest.mark.anyio
async def test_no_description_built_when_debug_is_off(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    describe = MagicMock()
    monkeypatch.setattr(mcp_client_context, "describe_mcp_client", describe)
    context = SimpleNamespace(
        message=SimpleNamespace(name="ha_get_state"), fastmcp_context=None
    )
    with caplog.at_level(logging.INFO, logger="ha_mcp.mcp_client_context"):
        await McpClientDebugLogMiddleware().on_call_tool(
            context, AsyncMock(return_value="ok")
        )
    describe.assert_not_called()
    assert caplog.text == ""


class TestExtractClientInfo:
    """Tests for extract_client_info — pulls client info off the MCP context."""

    def test_returns_empty_when_ctx_is_none(self):
        # Tool can be invoked outside an MCP request (unit tests, scripts).
        # Its callers must not blow up — empty dict is the contract.
        assert extract_client_info(None) == {}

    def test_reads_name_version_title_off_initialize_params(self):
        # Mirrors the vendored InitializeRequestParams.client_info (an
        # Implementation: name + version, optional title).
        client_info_obj = SimpleNamespace(
            name="Claude Desktop",
            version="0.7.42",
            title="Claude Desktop (claude.ai integration)",
        )
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(client_info=client_info_obj)
            )
        )
        info = extract_client_info(ctx)
        assert info == {
            "name": "Claude Desktop",
            "version": "0.7.42",
            "title": "Claude Desktop (claude.ai integration)",
        }

    def test_missing_optional_title_falls_back_to_empty_string(self):
        client_info_obj = SimpleNamespace(name="Cursor", version="0.42", title=None)
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(clientInfo=client_info_obj)
            )
        )
        info = extract_client_info(ctx)
        assert info["name"] == "Cursor"
        assert info["version"] == "0.42"
        assert info["title"] == ""

    def test_missing_version_falls_back_to_unknown(self):
        # Pin the docstring promise: name/version default to "unknown" when
        # the client didn't send them. If the future SDK refactor stops
        # sending `version`, the f"{name} {version}" downstream rendering
        # must not produce "Cursor None".
        client_info_obj = SimpleNamespace(name="Cursor", version=None, title=None)
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(clientInfo=client_info_obj)
            )
        )
        info = extract_client_info(ctx)
        assert info == {"name": "Cursor", "version": "unknown", "title": ""}

    def test_camel_case_client_info_attribute_is_picked_up(self):
        # The vendored SDK names it `client_info`; `clientInfo` is the
        # camelCase fallback for params that kept the wire spelling.
        client_info_obj = SimpleNamespace(
            name="Future Client", version="9.9.9", title=None
        )
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(clientInfo=client_info_obj)
            )
        )
        info = extract_client_info(ctx)
        assert info == {"name": "Future Client", "version": "9.9.9", "title": ""}

    def test_swallows_exceptions_and_returns_empty(self):
        # If the context shape is unexpected (future MCP / FastMCP API churn),
        # we'd rather degrade silently than fail the bug report.
        class Boom:
            # Use a property (not __getattr__) so the raise is a normal
            # attribute access, not a special-method protocol violation.
            # extract_client_info reads ``ctx.session`` first, so raising
            # here drives it into the broad ``except Exception`` swallow path.
            @property
            def session(self):
                raise RuntimeError("unexpected ctx shape")

        assert extract_client_info(Boom()) == {}


class TestExtractClientInfoUserAgentFallback:
    """Older-protocol clients over stateless HTTP send client info only in
    initialize, so the request's User-Agent identifies them instead."""

    @pytest.fixture
    def headers(self, monkeypatch):
        current: dict[str, str] = {}
        monkeypatch.setattr(
            mcp_client_context, "get_http_headers", lambda include=None: current
        )
        return current

    def _ctx_without_client_params(self):
        return SimpleNamespace(session=SimpleNamespace(client_params=None))

    def test_parses_product_and_version(self, headers):
        headers["user-agent"] = "claude-code/2.1.4 (external, cli)"
        assert extract_client_info(self._ctx_without_client_params()) == {
            "name": "claude-code",
            "version": "2.1.4",
            "title": "from HTTP User-Agent",
        }

    def test_product_without_version(self, headers):
        headers["user-agent"] = "SomeClient"
        assert extract_client_info(self._ctx_without_client_params()) == {
            "name": "SomeClient",
            "version": "unknown",
            "title": "from HTTP User-Agent",
        }

    def test_no_user_agent_returns_empty(self, headers):
        assert extract_client_info(self._ctx_without_client_params()) == {}

    def test_client_params_win_over_user_agent(self, headers):
        headers["user-agent"] = "python-httpx/0.28.1"
        client_info = SimpleNamespace(name="Cursor", version="1.0", title=None)
        ctx = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(client_info=client_info)
            )
        )
        assert extract_client_info(ctx)["name"] == "Cursor"


def test_extract_client_info_outside_an_http_request_returns_empty():
    """stdio has no HTTP request, so there is no User-Agent to fall back to."""
    ctx = SimpleNamespace(session=SimpleNamespace(client_params=None))
    assert extract_client_info(ctx) == {}
