"""In-HA probe for the component's LLM-API schema conversion (issue #2361).

The LLM-API E2E in ``tests/src/e2e/workflows/embedded/test_embedded_server.py``
drives the transport from the TEST HOST, so its schema conversion runs the
host's ``voluptuous_openapi`` — not the converter Home Assistant actually
resolves inside the container/VM. Core 2026.9 replaced that library with
``probatio`` and re-emits every converted schema through
``probatio.to_openapi`` before handing it to a conversation agent, which is
where the defect this probe exists to catch lives: a ``gt=`` bound turned into
the draft-4 ``{"minimum": 0, "exclusiveMinimum": true}`` shape and broke every
Anthropic turn.

``LLM_API_SCHEMA_PROBE`` is Python source executed by Home Assistant's OWN
interpreter (``python3`` inside the HA container), so it exercises the exact
component module, the exact converter Core installed, and the exact re-emission
the conversation integrations perform. It prints one sentinel-prefixed JSON
line; :func:`parse_probe_report` turns that back into a dict for the tests,
which own the pass/fail decision.
"""

from __future__ import annotations

import json
from typing import Any

#: Prefix of the single report line the probe prints on stdout.
PROBE_SENTINEL = "LLM_API_PROBE_REPORT"

#: Env var carrying the webhook URL into the probe process. ``sys.argv[1]``
#: takes precedence so the source can also be run by hand.
PROBE_URL_ENV = "HAMCP_PROBE_WEBHOOK_URL"

LLM_API_SCHEMA_PROBE = f"""
import asyncio
import json
import os
import signal
import sys

sys.path.insert(0, "/config")

from custom_components.ha_mcp_tools import llm_api

SENTINEL = "{PROBE_SENTINEL}"
URL_ENV = "{PROBE_URL_ENV}"
# The list/convert budget for the whole catalog; the caller's own exec timeout
# sits above it so a hang reports here rather than as an opaque kill.
TIMEOUT_S = 120

# Filled by _run; module-level so the timeout backstop can still print it.
REPORT = {{}}


def _walk(node):
    # Every mapping anywhere in a JSON-schema-shaped structure.
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _is_integer_node(node):
    declared = node.get("type")
    if isinstance(declared, str):
        return declared == "integer"
    if isinstance(declared, list):
        return "integer" in declared
    return False


def _count_integer_nodes(node):
    return sum(1 for sub in _walk(node) if _is_integer_node(sub))


def _boolean_exclusive_bounds(node):
    # Draft-4 spelling: the 2020-12 keywords carry a NUMBER, never a bool.
    for sub in _walk(node):
        for keyword in ("exclusiveMinimum", "exclusiveMaximum"):
            if isinstance(sub.get(keyword), bool):
                return True
    return False


def _short(err):
    text = str(err).strip().splitlines()
    head = text[0] if text else ""
    return "{{}}: {{}}".format(type(err).__name__, head[:200])


async def _run():
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(URL_ENV)
    if not url:
        raise SystemExit(
            "no webhook URL: pass it as the first argument or set " + URL_ENV
        )

    converter = llm_api._schema_converter()
    normaliser = getattr(llm_api, "_to_inclusive_bounds", None)
    # The production path, not a re-implementation of it: whatever
    # HaMcpLlmApi._convert_parameters does to a tool's schema before Core sees
    # it (normalisation included, once #2363 lands) is what this probe must
    # measure. It reads nothing from self, so the class attribute is called
    # directly. None means the component would have skipped the tool.
    convert_parameters = llm_api.HaMcpLlmApi._convert_parameters
    report = REPORT
    report.update({{
        "timed_out": False,
        "tool_count": 0,
        "converter": getattr(converter, "__module__", "") or repr(converter),
        "probatio": False,
        "inclusive_bounds_normaliser": normaliser is not None,
        "conversion_failures": [],
        "boolean_exclusive": [],
        "integer_lost": [],
        "draft2020_invalid": [],
        "ha_version": "unknown",
    }})

    try:
        from homeassistant.const import __version__ as ha_version
    except ImportError:
        pass
    else:
        report["ha_version"] = ha_version

    try:
        import probatio
    except ImportError:
        # Core <= 2026.8 still ships voluptuous_openapi and never re-emits.
        probatio = None
    report["probatio"] = probatio is not None

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        Draft202012Validator = None

    async with asyncio.timeout(TIMEOUT_S):
        async with llm_api._mcp_session(url) as (session, _init):
            tools = (await session.list_tools()).tools
        _convert_all(tools, convert_parameters, probatio, Draft202012Validator, report)

    print(SENTINEL + " " + json.dumps(report))


def _convert_all(tools, convert_parameters, probatio, Draft202012Validator, report):
    # Inside the caller's timeout on purpose: the conversion loop is the
    # operation under test, so a converter that hangs on one schema must
    # surface as the probe's own timeout, not as an opaque exec kill.
    report["tool_count"] = len(tools)
    for tool in tools:
        schema = tool.inputSchema
        try:
            params = convert_parameters(None, tool)
        except Exception as err:  # collecting, not suppressing
            report["conversion_failures"].append(tool.name + ": " + _short(err))
            continue
        if params is None:
            report["conversion_failures"].append(
                tool.name + ": _convert_parameters returned None (tool skipped)"
            )
            continue
        if probatio is None:
            continue
        try:
            # Exactly what homeassistant/components/anthropic/entity.py's
            # _format_tool hands the model: to_openapi in its default OpenAPI
            # 3.0 dialect, then the ROOT-level combinators dropped. Nested
            # ones stay, which is where the reported bound lived.
            emitted = probatio.to_openapi(params)
            emitted = {{
                key: value
                for key, value in emitted.items()
                if key not in ("oneOf", "anyOf", "allOf")
            }}
        except Exception as err:  # collecting, not suppressing
            report["conversion_failures"].append(
                tool.name + ": to_openapi: " + _short(err)
            )
            continue
        if _boolean_exclusive_bounds(emitted):
            report["boolean_exclusive"].append(tool.name)
        if _count_integer_nodes(emitted) < _count_integer_nodes(schema):
            report["integer_lost"].append(tool.name)
        if Draft202012Validator is not None:
            try:
                Draft202012Validator.check_schema(emitted)
            except Exception as err:  # collecting, not suppressing
                report["draft2020_invalid"].append(tool.name + ": " + _short(err))


class _ProbeTimeout(BaseException):
    # BaseException, not Exception, on purpose: the per-tool ``except
    # Exception`` handlers in _convert_all must not be able to swallow the
    # alarm, or the one-shot signal would be consumed and the probe would
    # carry on into the next blocking conversion and finish with
    # timed_out=False.
    pass


def _on_alarm(signum, frame):
    # asyncio.timeout can only cancel at an await; a converter that blocks
    # never reaches one. SIGALRM interrupts the main thread regardless, so
    # the probe still reports what it had instead of hanging until the exec
    # wrapper kills it with no output.
    raise _ProbeTimeout("probe exceeded " + str(TIMEOUT_S) + "s")


signal.signal(signal.SIGALRM, _on_alarm)
signal.alarm(TIMEOUT_S + 5)
try:
    asyncio.run(_run())
except _ProbeTimeout as err:
    REPORT["timed_out"] = True
    REPORT["conversion_failures"] = list(REPORT.get("conversion_failures", [])) + [
        "probe: " + str(err)
    ]
    print(SENTINEL + " " + json.dumps(REPORT))
    sys.exit(3)
finally:
    signal.alarm(0)
"""


def parse_probe_report(stdout: str) -> dict[str, Any]:
    """Return the probe's report dict from its captured stdout.

    Raises ``AssertionError`` with the raw output when the sentinel line is
    absent or unparseable — that means the probe died before printing, and the
    output is the only diagnosis available.
    """
    for line in stdout.splitlines():
        if not line.startswith(PROBE_SENTINEL):
            continue
        payload = line[len(PROBE_SENTINEL) :].strip()
        try:
            report = json.loads(payload)
        except json.JSONDecodeError as err:
            raise AssertionError(
                f"the in-HA LLM-API probe printed an unparseable report ({err}): "
                f"{payload[:500]}"
            ) from err
        if not isinstance(report, dict):
            raise AssertionError(
                f"the in-HA LLM-API probe printed a {type(report).__name__}, "
                f"not a report object: {payload[:500]}"
            )
        return report
    raise AssertionError(
        "the in-HA LLM-API probe printed no "
        f"{PROBE_SENTINEL} line; full output:\n{stdout[-4000:]}"
    )
