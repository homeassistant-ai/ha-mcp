"""The report's process probes: the MCP transport and the websockets copies.

Both read only the running process, so each branch is tested on its own;
``test_report_issue_environment.py`` shows their lines reach the report.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from ha_mcp._vendor import websockets as vendored_websockets
from ha_mcp.tools import tools_bug_report
from ha_mcp.tools.tools_bug_report import (
    detect_mcp_transport,
    format_websockets_dependency_value,
    websockets_dependency_state,
)


def _detached() -> bool:
    raise OSError("stdin closed")


@pytest.mark.parametrize(
    ("argv0", "env", "isatty", "transport"),
    [
        # The HTTP entry point wins over a transport variable saying stdio.
        pytest.param(
            "/usr/bin/ha-mcp-web",
            {"FASTMCP_TRANSPORT": "stdio"},
            lambda: False,
            "http",
            id="http-entry-point",
        ),
        # ha-mcp ships no SSE entry point; this run shape is unsupported and
        # worth surfacing.
        pytest.param(
            "/usr/bin/ha-mcp",
            {"FASTMCP_TRANSPORT": "sse"},
            lambda: False,
            "sse",
            id="env",
        ),
        pytest.param(
            "/usr/bin/ha-mcp",
            {"FASTMCP_TRANSPORT": "streamable-http"},
            lambda: False,
            "http",
            id="streamable-http",
        ),
        pytest.param(
            "/usr/bin/ha-mcp",
            {"FASTMCP_PORT": "8086"},
            lambda: False,
            "http",
            id="port-variable",
        ),
        # Home Assistant's own stdin is no terminal; the in-process server
        # only serves HTTP.
        pytest.param(
            "/usr/bin/python",
            {"HA_MCP_EMBEDDED": "1"},
            lambda: False,
            "http",
            id="embedded",
        ),
        # Supervisor starts the add-on without a terminal (#1322).
        pytest.param(
            "/usr/bin/python",
            {"SUPERVISOR_TOKEN": "supervisor-token"},
            lambda: False,
            "http",
            id="add-on",
        ),
        # Claude Desktop launching the server over a pipe.
        pytest.param("/usr/bin/ha-mcp", {}, lambda: False, "stdio", id="piped-stdin"),
        pytest.param("/usr/bin/ha-mcp", {}, lambda: True, "unknown", id="terminal"),
        # pythonw and daemonized runs have no usable stdin.
        pytest.param("/usr/bin/ha-mcp", {}, _detached, "unknown", id="detached-stdin"),
    ],
)
def test_transport_is_read_from_the_entry_point_environment_and_stdin(
    monkeypatch: pytest.MonkeyPatch,
    argv0: str,
    env: dict[str, str],
    isatty: Callable[[], bool],
    transport: str,
) -> None:
    """A wrong transport sends triage the wrong way."""
    for name in (
        "FASTMCP_TRANSPORT",
        "MCP_HTTP_PORT",
        "FASTMCP_PORT",
        "HA_MCP_EMBEDDED",
        "SUPERVISOR_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", [argv0])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=isatty))

    assert detect_mcp_transport() == transport


_REAL_IMPORT_MODULE = importlib.import_module
_REAL_METADATA_VERSION = importlib.metadata.version


def _broken_vendored_copy(name: str, package: str | None = None) -> Any:
    if name.startswith("ha_mcp._vendor.websockets"):
        raise ImportError("vendored copy damaged")
    return _REAL_IMPORT_MODULE(name, package)


def _shared_copy_raising(error: Exception) -> Callable[[str], str]:
    def version(name: str) -> str:
        if name == "websockets":
            raise error
        return _REAL_METADATA_VERSION(name)

    return version


def test_shipped_vendored_copy_reports_its_version_and_no_accelerator() -> None:
    """The vendor sync strips the compiled extension, so the shipped copy is
    always pure Python; vendoring the extension must update this on purpose."""
    state = websockets_dependency_state()

    assert state["vendored_import_ok"] is True
    assert state["vendored_version"] == vendored_websockets.__version__
    assert state["vendored_c_speedups"] is False


def test_damaged_vendored_copy_reports_the_import_error() -> None:
    with patch.object(
        tools_bug_report.importlib, "import_module", _broken_vendored_copy
    ):
        state = websockets_dependency_state()

    assert state["vendored_import_ok"] is False
    assert "vendored copy damaged" in state["vendored_import_error"]


@pytest.mark.parametrize(
    ("error", "unreadable"),
    [
        pytest.param(OSError("dist-info unreadable"), True, id="unreadable"),
        pytest.param(
            importlib.metadata.PackageNotFoundError("websockets"), False, id="absent"
        ),
    ],
)
def test_unreadable_shared_copy_is_told_apart_from_an_absent_one(
    error: Exception, unreadable: bool
) -> None:
    """Collapsing a corrupt shared install into "absent" reads as a clean
    environment during the failure class (#2135/#2146) the probe exists for."""
    with patch.object(
        tools_bug_report.importlib.metadata, "version", _shared_copy_raising(error)
    ):
        state = websockets_dependency_state()

    assert state["shared_metadata_version"] is None
    assert ("shared_metadata_error" in state) is unreadable


@pytest.mark.parametrize(
    ("state", "shown", "not_shown"),
    [
        pytest.param({}, "not probed", "vendored", id="not-probed"),
        # A "throughput dropped" report needs to see the accelerator is gone.
        pytest.param(
            {
                "vendored_import_ok": True,
                "vendored_version": "17.0.1",
                "vendored_c_speedups": False,
                "shared_metadata_version": "16.1.1",
            },
            "vendored 17.0.1 (pure-Python) | shared env copy: 16.1.1 per dist metadata",
            "absent",
            id="pure-python",
        ),
        pytest.param(
            {
                "vendored_import_ok": True,
                "vendored_version": "17.0.1",
                "vendored_c_speedups": True,
                "shared_metadata_version": "16.1.1",
            },
            "vendored 17.0.1",
            "pure-Python",
            id="accelerated",
        ),
        pytest.param(
            {
                "vendored_import_ok": False,
                "vendored_import_error": "ImportError: damaged",
                "shared_metadata_version": None,
            },
            "vendored BROKEN: ImportError: damaged | shared env copy: absent",
            "pure-Python",
            id="broken-and-absent",
        ),
        pytest.param(
            {
                "vendored_import_ok": True,
                "vendored_version": "17.0.1",
                "vendored_c_speedups": False,
                "shared_metadata_version": None,
                "shared_metadata_error": "OSError: dist-info unreadable",
            },
            "shared env copy: metadata unreadable: OSError: dist-info unreadable",
            "absent",
            id="unreadable",
        ),
    ],
)
def test_websockets_line_keeps_a_degraded_probe_visible(
    state: dict[str, Any], shown: str, not_shown: str
) -> None:
    """For #2135/#2146 this line in the pasted report is the diagnosis."""
    line = format_websockets_dependency_value({"websockets_dependency": state})

    assert shown in line
    assert not_shown not in line
