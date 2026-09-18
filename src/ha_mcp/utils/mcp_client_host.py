"""Identify the host application that launched this stdio MCP server.

The MCP ``initialize`` handshake only carries what the client chooses to
advertise. Claude Desktop, for example, sends ``local-agent-mode-<server>
1.0.0`` for every stdio server, so a bug report never learns which Desktop
release was involved -- and Desktop releases are exactly what client-side
regressions such as homeassistant-ai/ha-mcp#2472 hinge on. Over stdio the
host is an ancestor of this process, so its executable path (and, for the
Claude Desktop bundles, its version) can be read off the process tree.
"""

from __future__ import annotations

import logging
import plistlib
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Launch-chain intermediaries between the host app and ha-mcp: shells,
# package runners, interpreters, and Claude Desktop's own ``disclaimer``
# helper. Matched on the lowercased basename with any ``.exe``/``.cmd``
# suffix removed.
_WRAPPER_NAMES = frozenset(
    {
        "bash",
        "caffeinate",
        "cmd",
        "conhost",
        "dash",
        "disclaimer",
        "env",
        "fish",
        "ha-mcp",
        "login",
        "mcp-proxy",
        "mcp-remote",
        "node",
        "nohup",
        "npm",
        "npx",
        "powershell",
        "pwsh",
        "python",
        "pythonw",
        "sh",
        "uv",
        "uvx",
        "wsl",
        "wslhost",
        "zsh",
    }
)
_PYTHON_VERSIONED_RE = re.compile(r"^python\d")

# Claude Desktop on Windows: the Squirrel installer keeps each release under
# ``%LOCALAPPDATA%\AnthropicClaude\app-<version>\claude.exe`` (the same
# layout Claude Code's ``/desktop`` command version-gates on), and the MSIX
# package lands under ``WindowsApps\Claude_<version>_x64__<hash>\app\``.
_WINDOWS_VERSION_PATTERNS = (
    re.compile(r"[\\/]AnthropicClaude[\\/]app-(\d+(?:\.\d+)+)[\\/]"),
    re.compile(r"[\\/]WindowsApps[\\/]Claude_(\d+(?:\.\d+)+)_"),
)
_MAC_BUNDLE_MARKER = "/Claude.app/Contents/"


def detect_client_host() -> dict[str, str]:
    """Return ``{"name", "version", "evidence"}`` for the launching host app.

    Walks this process's ancestors nearest-first and reports the first one
    that is not a known launch-chain wrapper. Claude Desktop is recognised
    by its macOS bundle path or Windows install layout and gets a real
    version; any other host is reported by executable name with version
    ``unknown`` so the agent knows to ask the user. Returns ``{}`` when
    ``psutil`` is unavailable or the tree cannot be read -- never raises,
    since this runs inside the bug-report path that must stay robust.
    """
    try:
        import psutil
    except ImportError:
        logger.debug("psutil unavailable; skipping MCP client host detection")
        return {}
    try:
        ancestors = psutil.Process().parents()
    except psutil.Error as exc:
        logger.debug("Cannot read process ancestry: %s", exc)
        return {}
    for proc in ancestors:
        exe, name = _exe_and_name(proc, psutil)
        if not name:
            continue
        host = classify_host(exe, name)
        if host is not None:
            return host
    return {}


def _exe_and_name(proc: Any, psutil: Any) -> tuple[str, str]:
    """``(exe_path, basename)`` for a process; either may be empty.

    ``exe()`` raises ``AccessDenied`` for protected binaries such as MSIX
    packages under ``WindowsApps``; the bare ``name()`` still identifies
    the host, just without a path to derive a version from.
    """
    exe = ""
    name = ""
    try:
        exe = proc.exe() or ""
    except (psutil.Error, OSError):
        pass  # protected or vanished binary: fall back to the bare name
    try:
        name = proc.name() or ""
    except (psutil.Error, OSError):
        pass  # process gone mid-walk: skipped by the caller as nameless
    if not name and exe:
        name = Path(exe).name
    return exe, name


def classify_host(exe: str, name: str) -> dict[str, str] | None:
    """Classify one ancestor: a host-app record, or ``None`` for a wrapper."""
    if _MAC_BUNDLE_MARKER in exe:
        return {
            "name": "Claude Desktop",
            "version": _mac_bundle_version(exe) or "unknown",
            "evidence": exe,
        }
    windows_version = _windows_path_version(exe)
    if windows_version:
        return {"name": "Claude Desktop", "version": windows_version, "evidence": exe}
    stem = _stem(name)
    if stem in _WRAPPER_NAMES or _PYTHON_VERSIONED_RE.match(stem):
        return None
    # ``claude.exe`` is also Claude Code's Windows binary, so without the
    # Desktop install layout in the path only the bare name is reported.
    return {"name": name, "version": "unknown", "evidence": exe or name}


def _stem(name: str) -> str:
    lowered = name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def _mac_bundle_version(exe: str) -> str:
    """``CFBundleShortVersionString`` of the ``.app`` bundle containing ``exe``."""
    bundle_root = exe.split("/Contents/", 1)[0]
    plist_path = Path(bundle_root) / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        logger.debug("Cannot read %s: %s", plist_path, exc)
        return ""
    version = plist.get("CFBundleShortVersionString")
    return str(version) if version else ""


def _windows_path_version(exe: str) -> str:
    for pattern in _WINDOWS_VERSION_PATTERNS:
        match = pattern.search(exe)
        if match:
            return match.group(1)
    return ""
