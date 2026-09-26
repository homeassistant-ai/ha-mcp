"""Unit tests for the component's ``ha_mcp_tools/template_diagnose`` command.

Core's ``render_template`` reports a template error without the line it
happened on (#2522); the component renders the template in-process to recover
it. Jinja itself is not installed in the unit environment, so the failures
below are real Python exceptions shaped like Jinja's: a syntax error carrying
``lineno``, and a runtime error whose traceback passes through a frame compiled
under the ``<template>`` filename Jinja gives rewritten template frames.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

# Import through the sibling module: it installs the ``homeassistant.*`` stubs
# and the real voluptuous before any ``custom_components`` import.
from . import test_component_ws_search as _base
from .test_component_ws_search import (
    FakeHass,
    _FakeConnection,
    _FakeWSApi,
    _Unauthorized,
    wsapi,
)

_REAL_VOL = _base._REAL_VOL

from custom_components.ha_mcp_tools import template_diagnose  # noqa: E402


class _StubTemplateError(Exception):
    """Stand-in for core's ``TemplateError`` (``raise TemplateError(err) from err``)."""


class _SyntaxErrorWithLine(Exception):
    def __init__(self, message: str, lineno: int) -> None:
        super().__init__(message)
        self.lineno = lineno


def _runtime_error_at_template_line(line: int) -> BaseException:
    """A ZeroDivisionError raised from a ``<template>`` frame at ``line``."""
    code = compile("\n" * (line - 1) + "1 / 0\n", "<template>", "exec")
    try:
        exec(code, {})
    except ZeroDivisionError as err:
        return err
    raise AssertionError("unreachable: the compiled code always raises")


def _wrapped(err: BaseException) -> _StubTemplateError:
    try:
        raise _StubTemplateError(f"{type(err).__name__}: {err}") from err
    except _StubTemplateError as wrapper:
        return wrapper


class TestTemplateErrorLine:
    def test_syntax_error_uses_lineno(self) -> None:
        err = _wrapped(_SyntaxErrorWithLine("unexpected 'end of statement'", 3))
        assert template_diagnose.template_error_line(err) == 3

    def test_runtime_error_uses_the_template_frame(self) -> None:
        err = _wrapped(_runtime_error_at_template_line(4))
        assert template_diagnose.template_error_line(err) == 4

    def test_no_template_frame_means_no_line(self) -> None:
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError as err:
            assert template_diagnose.template_error_line(_wrapped(err)) is None

    def test_describe_failure_quotes_the_source_line(self) -> None:
        template = "line1\n{% set d = {'a': 1} %}\n{{ d.split('-') }}\nline4"
        err = _wrapped(_runtime_error_at_template_line(3))
        diagnosis = template_diagnose.describe_failure(template, "render", err)
        assert diagnosis == {
            "stage": "render",
            "error": "ZeroDivisionError: division by zero",
            "line": 3,
            "source_line": "{{ d.split('-') }}",
        }

    def test_leading_blank_lines_count_toward_the_line(self) -> None:
        """Core strips the template before compiling, so Jinja counts from the
        first non-blank line; the caller's own numbering is restored."""
        template = "\n\n  line one\n{{ d.split('-') }}\n"
        err = _wrapped(_runtime_error_at_template_line(2))
        diagnosis = template_diagnose.describe_failure(template, "render", err)
        assert diagnosis["line"] == 4
        assert diagnosis["source_line"] == "{{ d.split('-') }}"

    def test_line_past_the_template_end_has_no_source_line(self) -> None:
        err = _wrapped(_SyntaxErrorWithLine("unexpected end of template", 5))
        diagnosis = template_diagnose.describe_failure("{% if x %}", "compile", err)
        assert diagnosis["line"] == 5
        assert "source_line" not in diagnosis


class _FakeTemplate:
    """Core ``Template`` double driven by the failure a test pins on the class."""

    compile_error: BaseException | None = None
    render_error: BaseException | None = None
    will_timeout: bool = False
    calls: list[str]
    log_fns: list[Any]

    def __init__(self, template: str, hass: Any) -> None:
        self.template = template
        type(self).calls = []
        type(self).log_fns = []

    def ensure_valid(self) -> None:
        type(self).calls.append("ensure_valid")
        if self.compile_error is not None:
            raise self.compile_error

    async def async_render_will_timeout(
        self, timeout: float, variables: Any, strict: bool = False, log_fn: Any = None
    ) -> bool:
        type(self).calls.append(f"will_timeout:{timeout}:{variables}:{strict}")
        type(self).log_fns.append(log_fn)
        if self.render_error is not None:
            # Core re-raises a worker-thread error without its traceback.
            raise _StubTemplateError(str(self.render_error))
        return self.will_timeout

    def async_render(
        self, variables: Any, strict: bool = False, log_fn: Any = None
    ) -> Any:
        type(self).calls.append(f"render:{variables}:{strict}")
        type(self).log_fns.append(log_fn)
        if self.render_error is not None:
            raise self.render_error
        return "ok"


@pytest.fixture
def fake_core(monkeypatch: pytest.MonkeyPatch) -> type[_FakeTemplate]:
    fake = type("FakeTemplate", (_FakeTemplate,), {})
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.template", SimpleNamespace(Template=fake)
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.exceptions"],
        "TemplateError",
        _StubTemplateError,
        raising=False,
    )
    return fake


class TestAsyncDiagnose:
    @pytest.mark.asyncio
    async def test_compile_failure_stops_before_rendering(self, fake_core) -> None:
        fake_core.compile_error = _wrapped(_SyntaxErrorWithLine("bad", 2))
        diagnosis = await template_diagnose.async_diagnose(
            FakeHass(), "a\n{% if %}", None, False, 3.0
        )
        assert diagnosis == {
            "stage": "compile",
            "error": "_SyntaxErrorWithLine: bad",
            "line": 2,
            "source_line": "{% if %}",
        }
        assert fake_core.calls == ["ensure_valid"]

    @pytest.mark.asyncio
    async def test_runaway_template_is_not_rendered_on_the_loop(
        self, fake_core
    ) -> None:
        fake_core.will_timeout = True
        diagnosis = await template_diagnose.async_diagnose(
            FakeHass(), "{{ x }}", {"x": 1}, True, 2.0
        )
        assert diagnosis == {"stage": "timeout"}
        assert fake_core.calls == ["ensure_valid", "will_timeout:2.0:{'x': 1}:True"]

    @pytest.mark.asyncio
    async def test_render_failure_is_rerendered_for_its_line(self, fake_core) -> None:
        fake_core.render_error = _wrapped(_runtime_error_at_template_line(1))
        diagnosis = await template_diagnose.async_diagnose(
            FakeHass(), "{{ 1 / 0 }}", None, False, 3.0
        )
        assert diagnosis["stage"] == "render"
        assert diagnosis["line"] == 1
        assert diagnosis["source_line"] == "{{ 1 / 0 }}"
        assert fake_core.calls[-1] == "render:None:False"

    @pytest.mark.asyncio
    async def test_slow_failure_is_not_rendered_again_on_the_loop(
        self, fake_core, monkeypatch
    ) -> None:
        fake_core.render_error = _wrapped(_runtime_error_at_template_line(1))
        clock = iter([100.0, 100.0 + template_diagnose.LOOP_RERENDER_BUDGET_S + 5])
        monkeypatch.setattr(template_diagnose, "monotonic", lambda: next(clock))
        diagnosis = await template_diagnose.async_diagnose(
            FakeHass(), "{{ slow_then_fail }}", None, False, 30.0
        )
        assert diagnosis == {
            "stage": "render",
            "error": "ZeroDivisionError: division by zero",
        }
        assert not any(call.startswith("render:") for call in fake_core.calls)

    @pytest.mark.asyncio
    async def test_both_renders_keep_their_messages_out_of_the_log(
        self, fake_core
    ) -> None:
        """Without a log_fn Core logs undefined values at WARNING/ERROR."""
        fake_core.render_error = _wrapped(_runtime_error_at_template_line(1))
        await template_diagnose.async_diagnose(
            FakeHass(), "{{ d.split('-') }}", None, False, 3.0
        )
        assert fake_core.log_fns == [template_diagnose._discard_template_log] * 2
        assert template_diagnose._discard_template_log(40, "message") is None

    @pytest.mark.asyncio
    async def test_template_that_renders_reports_no_failure(self, fake_core) -> None:
        diagnosis = await template_diagnose.async_diagnose(
            FakeHass(), "{{ 1 }}", None, False, 3.0
        )
        assert diagnosis == {"stage": "none"}


class TestRegistration:
    def test_capability_is_advertised(self) -> None:
        assert "template_diagnose" in wsapi._do_info(FakeHass())["capabilities"]

    def test_registered_and_admin_gated(self, fake_core, monkeypatch) -> None:
        transport = _FakeWSApi()
        monkeypatch.setattr(wsapi, "websocket_api", transport)
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        wsapi.async_register_commands(FakeHass())
        handler = transport.registered[wsapi.WS_TEMPLATE_DIAGNOSE]
        msg = {
            "id": 1,
            "type": wsapi.WS_TEMPLATE_DIAGNOSE,
            "template": "{{ 1 }}",
            "strict": False,
            "timeout": 3.0,
        }
        with pytest.raises(_Unauthorized):
            handler(FakeHass(), _FakeConnection(is_admin=False), msg)
        connection = _FakeConnection()
        handler(FakeHass(), connection, msg)
        assert connection.results[1] == {"stage": "none"}


class TestSchema:
    def test_defaults_and_coercion(self, monkeypatch) -> None:
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._template_diagnose_schema())
        out = schema({"type": wsapi.WS_TEMPLATE_DIAGNOSE, "template": "{{ 1 }}"})
        assert out["strict"] is False
        assert out["timeout"] == 3.0
        assert (
            schema({"type": wsapi.WS_TEMPLATE_DIAGNOSE, "template": "x", "timeout": 5})[
                "timeout"
            ]
            == 5.0
        )

    @pytest.mark.parametrize("timeout", [0, 61, -1])
    def test_rejects_out_of_range_timeout(self, monkeypatch, timeout) -> None:
        monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
        schema = _REAL_VOL.Schema(wsapi._template_diagnose_schema())
        with pytest.raises(_REAL_VOL.Invalid):
            schema(
                {
                    "type": wsapi.WS_TEMPLATE_DIAGNOSE,
                    "template": "x",
                    "timeout": timeout,
                }
            )
