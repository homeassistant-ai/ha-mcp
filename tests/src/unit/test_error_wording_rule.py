"""Production code decides by an error's code, status or type, not its wording.

``scripts/error_wording.py`` runs here on small sources written for each case.
The whole checkout is scanned by the Fast Checks step that runs the script.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "error_wording.py"


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("error_wording_rule", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _found(guard: ModuleType, source: str, path: str = "src/ha_mcp/tools/x.py"):
    return guard.scan({path: textwrap.dedent(source)})


REFUSED = {
    "substring of a caught exception": """
        try:
            call()
        except Exception as err:
            if "not found" in str(err).lower():
                skip()
        """,
    "prefix of a reply's error": """
        def handle(reply):
            message = str(reply.get("error", ""))
            if message.startswith("Path not allowed"):
                return None
        """,
    "equality with prose": """
        def handle(reply):
            if reply["error"].removeprefix("Command failed: ") == "Unauthorized":
                raise PermissionError
        """,
    "regex over the message": """
        import re
        def handle(error: Exception):
            return re.search(r"belongs to (\\S+)", str(error))
        """,
    "loop over fixed markers": """
        MARKERS = ("timed out", "connection reset")
        def transient(exc: Exception) -> bool:
            lowered = str(exc).lower()
            return any(marker in lowered for marker in MARKERS)
        """,
    "membership in a set of messages": """
        NO_CONFIG = frozenset({"No config found."})
        def missing(reply):
            return reply.get("error") in NO_CONFIG
        """,
    "text passed to a helper": """
        def is_missing(text):
            return text.endswith("No prefs")
        def read(reply):
            return is_missing(str(reply.get("error", "")))
        """,
    "text returned by a helper": """
        def message_of(reply):
            return reply.get("error") or ""
        def read(reply):
            return "unknown command" in message_of(reply)
        """,
    "nested function": """
        def classify(exc: Exception):
            text = str(exc)
            def has(*needles):
                return any(needle in text for needle in needles)
            return has("MemoryError")
        """,
    "the message inside an error object": """
        def handle(reply):
            error = reply.get("error")
            return error.get("message") == "Not supported"
        """,
    "text passed by keyword": """
        def is_missing(*, text):
            return "No prefs" in text
        def read(reply):
            return is_missing(text=str(reply.get("error", "")))
        """,
    "the first argument of an exception": """
        def handle(err: Exception):
            return err.args[0] == "Unknown entity"
        """,
    "text built into an f-string": """
        def handle(err: Exception):
            line = f"failed: {err}"
            return "timed out" in line
        """,
    "the message of a REST reply": """
        def handle(error):
            return error.response_data["message"] == "Resource not found"
        """,
    "equality with a constant that holds prose": """
        NOT_SUPPORTED = "Not supported"
        def handle(reply):
            return reply.get("error") == NOT_SUPPORTED
        """,
    "an exception reached through its cause": """
        def handle(err: Exception):
            cause = err.__cause__
            return "expired" in str(cause)
        """,
    "an exception taken from a task": """
        def handle(task):
            failure = task.exception()
            return str(failure).startswith("Timed out")
        """,
    "a match on the text": """
        def handle(err: Exception):
            match str(err):
                case "Unauthorized" | "Forbidden":
                    return True
            return False
        """,
}

ALLOWED = {
    "error code": """
        def handle(reply):
            return reply.get("error_code") == "not_found"
        """,
    "code-like value of the error field": """
        def handle(reply):
            return reply.get("error") == "describe_timeout"
        """,
    "exception type and status": """
        def handle(exc: Exception):
            return isinstance(exc, TimeoutError) or exc.status_code == 404
        """,
    "a runtime value searched in the text": """
        def scrub(reply, secrets):
            text = str(reply.get("error", ""))
            for secret in secrets:
                if secret in text:
                    text = text.replace(secret, "<redacted>")
            return text
        """,
    "a log entry's message": """
        def search(entries, needle):
            return [e for e in entries if needle in str(e.get("message", "")).lower()]
        """,
    "an empty error": """
        def handle(reply):
            return reply.get("error") == ""
        """,
    "equality with a constant that holds a code": """
        TIMED_OUT = "describe_timeout"
        def handle(reply):
            return reply.get("error") == TIMED_OUT
        """,
    "the error field among bare codes": """
        def handle(reply):
            return reply.get("error") in ("cards_loading", "card_not_inspected")
        """,
    "a match on the code": """
        def handle(reply):
            match reply.get("error_code"):
                case "not_found":
                    return True
            return False
        """,
}


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_a_decision_by_wording_is_refused(guard: ModuleType, source: str) -> None:
    assert len(_found(guard, source)) == 1


@pytest.mark.parametrize("source", ALLOWED.values(), ids=ALLOWED.keys())
def test_a_code_type_or_runtime_value_passes(guard: ModuleType, source: str) -> None:
    assert _found(guard, source) == []


@pytest.mark.parametrize(
    "path",
    ["src/ha_mcp/error_wording.py", "custom_components/ha_mcp_tools/error_wording.py"],
)
def test_the_wording_module_is_the_one_place_for_it(
    guard: ModuleType, path: str
) -> None:
    source = REFUSED["substring of a caught exception"]

    assert _found(guard, source, path) == []
    assert len(_found(guard, source, path.replace("error_wording", "other"))) == 1
    # Only those two files: the name alone exempts nothing.
    assert len(_found(guard, source, "src/ha_mcp/tools/error_wording.py")) == 1


def test_a_finding_names_the_file_and_line(guard: ModuleType) -> None:
    found = _found(guard, REFUSED["prefix of a reply's error"])

    assert found == ["src/ha_mcp/tools/x.py:4: message.startswith('Path not allowed')"]
