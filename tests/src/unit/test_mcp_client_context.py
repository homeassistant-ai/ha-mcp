"""Per-request MCP client description: protocol version and declared capabilities."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.mcp_types import (
    ClientCapabilities,
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
        extensions={"io.example/b": {}, "io.example/a": {}},
    )
    assert capability_names(caps) == [
        "elicitation.form",
        "elicitation.url",
        "sampling",
        "extension:io.example/a",
        "extension:io.example/b",
    ]
    assert capability_names(None) == []


def test_describe_without_a_request() -> None:
    assert describe_mcp_client(None) == {
        "protocol_version": "unknown",
        "client_info": {},
        "capabilities": [],
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
    from ha_mcp import mcp_client_context

    describe = AsyncMock()
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
