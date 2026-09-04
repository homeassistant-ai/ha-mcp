"""In-HA probe for the component's LLM-API schema conversion (issue #2361).

The LLM-API E2E in ``tests/src/e2e/workflows/embedded/test_embedded_server.py``
used to convert every tool schema in the pytest process against the test host's
``voluptuous_openapi``. That proved nothing about Home Assistant: since Core
2026.9 the converter HA resolves is ``probatio``, and before a conversation
turn reaches the model Core re-emits every converted schema through
``probatio.to_openapi``. That round trip is invisible from the host and is where
the reported defect lived: a ``gt=`` bound came back in the draft-4
``{"minimum": 0, "exclusiveMinimum": true}`` spelling and broke every Anthropic
turn. The host-side loop was removed; this probe replaces it.

``LLM_API_SCHEMA_PROBE`` is Python source executed by Home Assistant's OWN
interpreter (``python3`` inside the HA container), so it exercises the exact
component module, the exact converter Core installed, and the exact re-emission
the conversation integrations perform. It prints one sentinel-prefixed JSON
line; :func:`parse_probe_report` turns that back into a dict and
:func:`assert_report_clean` owns the pass/fail decision, so both halves are
unit-testable without a lane.
"""

from __future__ import annotations

import json
from typing import Any

#: Prefix of the single report line the probe prints on stdout.
PROBE_SENTINEL = "LLM_API_PROBE_REPORT"

#: Env var carrying the webhook URL into the probe process. ``sys.argv[1]``
#: takes precedence so the source can also be run by hand.
PROBE_URL_ENV = "HAMCP_PROBE_WEBHOOK_URL"

#: Exit status the probe uses when it printed a partial report after a timeout.
PROBE_TIMEOUT_EXIT = 3

#: Minimum tool inventory the probe must see; the catalog has 88 tools and the
#: no-tools lanes still expose well above this.
MIN_TOOL_COUNT = 60

#: Pure helpers the probe runs on schemas. Kept as a separate source string so
#: the unit tier can ``exec`` them without the runner's imports and side effects.
PROBE_HELPERS_SOURCE = """
import traceback

# Mirrors the component: the values under these keys are instance data or
# vendor extensions that llm_api._INSTANCE_VALUES / llm_api._is_opaque_key copy
# through untouched, so a bound-like key inside them is not a bound and an
# integer type inside them is not a parameter type.
_OPAQUE_VALUE_KEYS = ("default", "const", "enum", "examples", "example")


def _walk(node):
    # Every SUBSCHEMA mapping anywhere in a JSON-schema-shaped structure.
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key in _OPAQUE_VALUE_KEYS or key.startswith("x-"):
                continue
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
    # One line: type, first message line, and the innermost frame, which is
    # the only location a remote interpreter can hand back.
    text = str(err).strip().splitlines()
    head = text[0] if text else ""
    frames = traceback.extract_tb(err.__traceback__) if err.__traceback__ else []
    where = ""
    if frames:
        last = frames[-1]
        where = " at {}:{} in {}".format(last.filename, last.lineno, last.name)
    return "{}: {}{}".format(type(err).__name__, head[:200], where)
"""

_PROBE_RUNNER_SOURCE = f"""
import asyncio
import json
import os
import signal
import sys

sys.path.insert(0, "/config")

from custom_components.ha_mcp_tools import llm_api

SENTINEL = "{PROBE_SENTINEL}"
URL_ENV = "{PROBE_URL_ENV}"
TIMEOUT_EXIT = {PROBE_TIMEOUT_EXIT}
# The list/convert budget for the whole catalog; both callers pass an exec
# timeout above it so a hang reports here rather than as an opaque kill.
TIMEOUT_S = 120

# Filled by _run; module-level so the timeout backstop can still print it.
REPORT = {{}}


async def _run():
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(URL_ENV)
    if not url:
        raise SystemExit(
            "no webhook URL: pass it as the first argument or set " + URL_ENV
        )

    converter = llm_api._schema_converter()
    # The production path, not a re-implementation of it: the mirror paths
    # normalise once per tool (_normalise_schema) and hand the result to
    # HaMcpLlmApi._convert_parameters(self, tool, schema). That method reads
    # nothing from self, so the class attribute is called directly. None
    # means the component would have skipped the tool.
    convert_parameters = llm_api.HaMcpLlmApi._convert_parameters
    normalise_schema = getattr(llm_api, "_normalise_schema", None)
    report = REPORT
    report.update({{
        "timed_out": False,
        "tool_count": 0,
        "converter": getattr(converter, "__module__", "") or repr(converter),
        "probatio": False,
        "probatio_import_error": None,
        "draft2020_checked": False,
        "jsonschema_import_error": None,
        "normalise_schema": normalise_schema is not None,
        "conversion_failures": [],
        "boolean_exclusive": [],
        "integer_lost": [],
        "draft2020_invalid": [],
        "ha_version": "unknown",
        "ha_version_error": None,
    }})

    try:
        from homeassistant.const import __version__ as ha_version
    except ImportError as err:
        report["ha_version_error"] = _short(err)
    else:
        report["ha_version"] = ha_version

    try:
        import probatio
    except ImportError as err:
        # probatio is not importable, so no re-emission step exists in this
        # interpreter. Whether that is legitimate is the caller's call, from
        # ha_version: Core ships probatio from 2026.9.
        probatio = None
        report["probatio_import_error"] = _short(err)
    report["probatio"] = probatio is not None

    try:
        from jsonschema import Draft202012Validator
    except ImportError as err:
        Draft202012Validator = None
        report["jsonschema_import_error"] = _short(err)
    report["draft2020_checked"] = Draft202012Validator is not None

    if normalise_schema is None:
        report["conversion_failures"].append(
            "probe: the component under test has no _normalise_schema; the "
            "probe cannot measure the production path"
        )
        print(SENTINEL + " " + json.dumps(report))
        return

    async with asyncio.timeout(TIMEOUT_S):
        async with llm_api._mcp_session(url) as (session, _init):
            tools = (await session.list_tools()).tools
        _convert_all(
            tools,
            convert_parameters,
            normalise_schema,
            probatio,
            Draft202012Validator,
            report,
        )

    print(SENTINEL + " " + json.dumps(report))


def _convert_all(
    tools, convert_parameters, normalise_schema, probatio, Draft202012Validator, report
):
    # Inside the caller's timeout on purpose: the conversion loop is the
    # operation under test, so a converter that hangs on one schema must
    # surface as the probe's own timeout, not as an opaque exec kill.
    report["tool_count"] = len(tools)
    for tool in tools:
        schema = tool.inputSchema
        try:
            params = convert_parameters(None, tool, normalise_schema(schema, tool.name))
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
except (TimeoutError, _ProbeTimeout) as err:
    # TimeoutError is the asyncio budget firing at an await (a stalled
    # list_tools); _ProbeTimeout is the alarm firing in blocking code. Both
    # print the partial report so the stage that stalled is on record.
    REPORT["timed_out"] = True
    REPORT["conversion_failures"] = list(REPORT.get("conversion_failures", [])) + [
        "probe: " + (str(err) or type(err).__name__)
    ]
    print(SENTINEL + " " + json.dumps(REPORT))
    sys.exit(TIMEOUT_EXIT)
finally:
    signal.alarm(0)
"""

#: The complete probe: helpers first, then the runner that uses them.
LLM_API_SCHEMA_PROBE = PROBE_HELPERS_SOURCE + _PROBE_RUNNER_SOURCE


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


def _year_month(version: str) -> tuple[int, int] | None:
    """``(2026, 9)`` for ``"2026.9.0"``; None when the prefix is not numeric."""
    parts = version.split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return int(parts[0]), int(parts[1])


def assert_report_clean(report: dict[str, Any]) -> None:
    """Fail with the offending tool names when the in-HA conversion misbehaved.

    Pure: takes the parsed report, raises ``AssertionError``. The order is the
    order a maintainer wants the diagnosis in: a timed-out or mis-run probe
    first, then the inventory, then the conversion, then the re-emission.
    """
    converter = report.get("converter")
    ha_version = str(report.get("ha_version", "unknown"))
    context = (
        f"converter={converter!r}, probatio={report.get('probatio')!r}, "
        f"normalise_schema={report.get('normalise_schema')!r}, "
        f"HA {ha_version}"
    )

    assert not report.get("timed_out"), (
        f"the in-HA probe hit its own timeout ({context}); partial report: {report}"
    )

    assert ha_version != "unknown", (
        "the probe could not read Home Assistant's version "
        f"({report.get('ha_version_error')!r}); it is not running in HA's "
        f"interpreter, so nothing else in this report is trustworthy ({context})"
    )

    tool_count = report.get("tool_count", 0)
    assert tool_count > MIN_TOOL_COUNT, (
        f"expected the full tool inventory from inside HA, got {tool_count} "
        f"({context}) — a handful would mean a truncated or wrong server"
    )

    failures = report.get("conversion_failures") or []
    assert not failures, (
        f"{len(failures)}/{tool_count} tool schemas failed to convert inside "
        f"Home Assistant ({context}). At runtime each of these is skipped with "
        "only a warning, so the toolset would silently shrink:\n" + "\n".join(failures)
    )

    if not report.get("probatio"):
        # Without probatio there is no re-emission step, so conversion success
        # is the whole contract. That is only legitimate on a Core that
        # predates probatio; from 2026.9 its absence means a broken image.
        year_month = _year_month(ha_version)
        assert year_month is not None and year_month < (2026, 9), (
            f"probatio is not importable on HA {ha_version} "
            f"({report.get('probatio_import_error')!r}) — from 2026.9 Core "
            "re-emits every schema through it, so this is a broken image, not "
            f"an image without the re-emission step ({context})"
        )
        return

    assert report.get("draft2020_checked"), (
        "jsonschema was not importable inside Home Assistant "
        f"({report.get('jsonschema_import_error')!r}), so the draft 2020-12 "
        "validation — the check that reproduces the reported Anthropic "
        f"failure — never ran ({context})"
    )

    boolean_exclusive = report.get("boolean_exclusive") or []
    assert not boolean_exclusive, (
        "probatio.to_openapi re-emitted a BOOLEAN exclusiveMinimum/"
        "exclusiveMaximum (the draft-4 spelling) for "
        f"{boolean_exclusive} ({context}) — that is what breaks every "
        "conversation turn on an agent that validates draft 2020-12"
    )

    integer_lost = report.get("integer_lost") or []
    assert not integer_lost, (
        f"the schema re-emitted through {converter!r} and probatio.to_openapi "
        f"has fewer integer-typed nodes than the tool's own schema for "
        f"{integer_lost} ({context}) — a count comparison, so a lost integer "
        "type may have been masked by an added one, but a lower count is "
        "always a looser schema than the tool accepts"
    )

    draft_invalid = report.get("draft2020_invalid") or []
    assert not draft_invalid, (
        "the re-emitted schema is not valid JSON Schema draft 2020-12 for "
        f"{len(draft_invalid)} tool(s) ({context}):\n" + "\n".join(draft_invalid)
    )
