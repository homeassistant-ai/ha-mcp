"""The component's ``ha_mcp_tools/template_diagnose`` command on a real ``hass``.

Core's ``render_template`` reports a template error without the line it
happened on (#2522); the component renders the template in-process to recover
it. The templates below are rendered by Core's own template engine, through
Core's WebSocket API (``component_ws``).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import websocket_api as core_websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import template_diagnose
from custom_components.ha_mcp_tools.websocket_api.constants import (
    WS_INFO,
    WS_TEMPLATE_DIAGNOSE,
)

from ._component_core import ws_call, ws_result

# A dict has no ``split``: Core's engine raises when the line renders.
FAILS_ON_LINE_3 = "line1\n{% set d = {'a': 1} %}\n{{ d.split('-') }}\nline4"
# Long enough to outlast any timeout the command accepts.
RUNAWAY = (
    "{% for i in range(1000) %}{% for j in range(1000) %}"
    "{% for k in range(1000) %}{% endfor %}{% endfor %}{% endfor %}"
)


async def _diagnose(ws: MockHAClientWebSocket, template: str, **params: Any) -> Any:
    return await ws_result(ws, WS_TEMPLATE_DIAGNOSE, template=template, **params)


async def test_a_syntax_error_names_its_line(
    component_ws: MockHAClientWebSocket,
) -> None:
    """Compiling fails, so nothing is rendered."""
    diagnosis = await _diagnose(component_ws, "a\nb\n{% if %}")

    assert diagnosis["stage"] == "compile"
    assert (diagnosis["line"], diagnosis["source_line"]) == (3, "{% if %}")


async def test_a_render_error_names_and_quotes_its_line(
    component_ws: MockHAClientWebSocket,
) -> None:
    diagnosis = await _diagnose(component_ws, FAILS_ON_LINE_3)

    assert diagnosis["stage"] == "render"
    assert "split" in diagnosis["error"]
    assert (diagnosis["line"], diagnosis["source_line"]) == (3, "{{ d.split('-') }}")


async def test_leading_blank_lines_count_toward_the_line(
    component_ws: MockHAClientWebSocket,
) -> None:
    """Core strips the template before compiling, so Jinja counts from the
    first non-blank line; the caller's own numbering is restored."""
    template = "\n\n{% set d = {'a': 1} %}\n{{ d.split('-') }}\n"

    diagnosis = await _diagnose(component_ws, template)

    assert (diagnosis["line"], diagnosis["source_line"]) == (4, "{{ d.split('-') }}")


async def test_a_runaway_template_times_out_instead_of_rendering_on_the_loop(
    component_ws: MockHAClientWebSocket,
) -> None:
    with patch.object(
        Template, "async_render", autospec=True, side_effect=Template.async_render
    ) as on_loop:
        diagnosis = await _diagnose(component_ws, RUNAWAY, timeout=0.1)

    assert diagnosis == {"stage": "timeout"}
    on_loop.assert_not_called()


async def test_a_slow_failure_is_not_rendered_again_on_the_loop(
    component_ws: MockHAClientWebSocket, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeating a slow failing render would block Home Assistant as long
    again; the failure is reported without its line instead."""
    clock = iter([100.0, 100.0 + template_diagnose.LOOP_RERENDER_BUDGET_S + 5])
    monkeypatch.setattr(template_diagnose, "monotonic", lambda: next(clock))

    with patch.object(
        Template, "async_render", autospec=True, side_effect=Template.async_render
    ) as on_loop:
        diagnosis = await _diagnose(component_ws, FAILS_ON_LINE_3)

    assert diagnosis["stage"] == "render"
    assert "line" not in diagnosis
    on_loop.assert_not_called()


async def test_the_diagnosis_renders_keep_their_messages_out_of_the_log(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Core logs an undefined variable at WARNING when the caller gives no
    log function; the caller already has that message from render_template."""
    template = "{{ missing_var }}\n{% set d = {'a': 1} %}{{ d.split('-') }}"
    with caplog.at_level(logging.WARNING):
        Template("{{ missing_var }}", hass).async_render()
    assert "missing_var" in caplog.text
    caplog.clear()

    with caplog.at_level(logging.WARNING):
        diagnosis = await _diagnose(component_ws, template)

    assert diagnosis["stage"] == "render"
    assert "missing_var" not in caplog.text


async def test_a_template_that_renders_reports_no_failure(
    component_ws: MockHAClientWebSocket,
) -> None:
    assert await _diagnose(component_ws, "{{ 1 }}") == {"stage": "none"}


async def test_the_capability_is_advertised(
    component_ws: MockHAClientWebSocket,
) -> None:
    info = await ws_result(component_ws, WS_INFO)

    assert "template_diagnose" in info["capabilities"]


async def test_only_an_administrator_can_diagnose(
    component_ws_read_only: MockHAClientWebSocket,
) -> None:
    reply = await ws_call(component_ws_read_only, WS_TEMPLATE_DIAGNOSE, template="1")

    assert reply["error"]["code"] == "unauthorized"


async def test_omitted_options_default_and_the_timeout_is_a_float(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    schema = hass.data[core_websocket_api.DOMAIN][WS_TEMPLATE_DIAGNOSE][1]
    message = {"id": 1, "type": WS_TEMPLATE_DIAGNOSE, "template": "{{ 1 }}"}

    defaults = schema(message)
    coerced = schema({**message, "timeout": 5})

    assert (defaults["strict"], defaults["timeout"]) == (False, 3.0)
    assert isinstance(coerced["timeout"], float)
    assert coerced["timeout"] == 5.0


@pytest.mark.parametrize("timeout", [0, 61, -1])
async def test_an_out_of_range_timeout_is_refused(
    component_ws: MockHAClientWebSocket, timeout: int
) -> None:
    reply = await ws_call(
        component_ws, WS_TEMPLATE_DIAGNOSE, template="x", timeout=timeout
    )

    assert reply["error"]["code"] == "invalid_format"
