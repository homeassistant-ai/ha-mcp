"""Locate where a Home Assistant template fails, for ``ha_eval_template``.

Core's ``render_template`` WebSocket command reports a template error by its
message alone, so an agent debugging a long template has to bisect it to find
the failing line (#2522). Rendering the same template in-process keeps the
Jinja exception: a syntax error carries ``lineno``, and a runtime error's
traceback passes through a frame Jinja rewrites to ``<template>`` at the
template's own line number.

The render mirrors Core's: ``async_render_will_timeout`` first renders in a
worker thread under the caller's timeout, so a runaway template is reported as
timed out and never rendered on the event loop.
"""

from __future__ import annotations

from time import monotonic
from typing import Any

JINJA_TEMPLATE_FILENAME = "<template>"

# A failed render is repeated on the event loop only when the worker-thread
# render showed it fails this quickly; Core has already rendered it on the loop
# once for render_template, and a slow failure repeated would block Home
# Assistant for as long again. Such a failure is reported without its line.
LOOP_RERENDER_BUDGET_S = 1.0


def _discard_template_log(level: int, message: str) -> None:
    """Keep the diagnosis renders' undefined-variable messages out of HA's log.

    Without a ``log_fn`` Core writes them to Home Assistant's log at WARNING or
    ERROR. The caller already has them from ``render_template``, or, when
    ``report_errors`` is off, Core has already written them to its log itself;
    either way logging them again only adds noise.
    """


def template_error_line(exc: BaseException) -> int | None:
    """Return the 1-based line a failure points at, or None if it names none.

    Counted in the template Core compiled, which it strips of leading and
    trailing whitespace. A runtime error inside a macro passes through several
    ``<template>`` frames; the innermost one is where it happened.
    """
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    lineno = getattr(cause, "lineno", None)
    if isinstance(lineno, int):
        return lineno
    line = None
    tb = cause.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == JINJA_TEMPLATE_FILENAME:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def describe_failure(template: str, stage: str, exc: BaseException) -> dict[str, Any]:
    """The diagnosis for a template that failed at ``stage``.

    ``line`` is renumbered from the stripped template Core compiled to the
    caller's own text, so a template pasted with leading blank lines still
    points at the line the caller wrote.
    """
    diagnosis: dict[str, Any] = {"stage": stage, "error": str(exc)}
    line = template_error_line(exc)
    if line is not None:
        line += template[: len(template) - len(template.lstrip())].count("\n")
        diagnosis["line"] = line
        lines = template.splitlines()
        if 0 < line <= len(lines):
            diagnosis["source_line"] = lines[line - 1].strip()
    return diagnosis


async def async_diagnose(
    hass: Any,
    template: str,
    variables: dict[str, Any] | None,
    strict: bool,
    timeout: float,
) -> dict[str, Any]:
    """Render ``template`` and report the stage and line it fails at, if any."""
    from homeassistant.exceptions import TemplateError
    from homeassistant.helpers.template import Template

    tpl = Template(template, hass)
    try:
        tpl.ensure_valid()
    except TemplateError as err:
        return describe_failure(template, "compile", err)

    started = monotonic()
    try:
        if await tpl.async_render_will_timeout(
            timeout, variables, strict=strict, log_fn=_discard_template_log
        ):
            return {"stage": "timeout"}
    except TemplateError as err:
        if monotonic() - started > LOOP_RERENDER_BUDGET_S:
            return {"stage": "render", "error": str(err)}
        # Core flattens the worker thread's error into a message, dropping the
        # traceback that names the line; the render below repeats it on the
        # loop to keep it.

    try:
        tpl.async_render(variables, strict=strict, log_fn=_discard_template_log)
    except TemplateError as err:
        return describe_failure(template, "render", err)
    return {"stage": "none"}
