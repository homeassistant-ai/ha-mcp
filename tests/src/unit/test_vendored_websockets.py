"""Guards for the vendored websockets copy (issues #2135/#2146).

ha-mcp imports ONLY its private ``ha_mcp._vendor.websockets`` copy, never
the shared site-packages one: inside Home Assistant the shared copy is
unowned — ~20 integration libraries (ring-doorbell, samsungtvws,
homematicip, google-genai, ...) drag it in with conflicting version
demands, and any of their installs can replace or tear it in place. The
vendored copy is immune to all of it, and CI always tests exactly the
version production runs.

Three guards keep the design honest:
1. The vendored tree matches the renovate-managed pin — a version bump
   that forgets to run ``scripts/vendor_websockets.py`` cannot merge.
2. The API surface our client code uses exists in the vendored copy.
3. Nothing under ``src/ha_mcp`` (outside ``_vendor``) imports the shared
   ``websockets`` — the regression that would silently re-enter the
   shared-copy war.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from ha_mcp._vendor import websockets
from ha_mcp._vendor.websockets.asyncio.client import ClientConnection
from ha_mcp._vendor.websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    WebSocketException,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "ha_mcp"
_VENDOR = _SRC / "_vendor"


def _pinned_version() -> str:
    for line in (_VENDOR / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"websockets==([A-Za-z0-9.!+-]+)", line.strip()):
            return match.group(1)
    raise AssertionError("no websockets pin in _vendor/requirements.txt")


class TestVendoredTreeMatchesPin:
    def test_module_version_matches_the_pin(self):
        assert websockets.__version__ == _pinned_version(), (
            "vendored tree drifted from the pin — run "
            "scripts/vendor_websockets.py and commit the result"
        )

    def test_vendored_marker_matches_the_pin(self):
        marker = (_VENDOR / "websockets" / "VENDORED").read_text(encoding="utf-8")
        assert f"websockets=={_pinned_version()}" in marker

    def test_license_ships_with_the_vendored_tree(self):
        assert (_VENDOR / "websockets" / "LICENSE").is_file()


class TestVendoredApiSurface:
    """Every websockets API our code uses exists in the vendored copy."""

    def test_connect_and_connection_exist(self):
        assert callable(websockets.connect)
        assert isinstance(websockets.ClientConnection, type)
        assert isinstance(ClientConnection, type)

    def test_connect_accepts_every_kwarg_we_pass(self):
        # The union of kwargs used at websocket_client.py::connect and
        # tools_addons.py. Asserted as EXPLICIT parameters (not swallowed by
        # a **kwargs catch-all, which forwards typos/renames to
        # create_connection and detonates at runtime); ``ssl`` is genuinely
        # a forwarded create_connection kwarg, so it is exempt.
        used_kwargs = {
            "ping_interval",
            "ping_timeout",
            "additional_headers",
            "max_size",
            "open_timeout",
            "close_timeout",
        }
        params = set(inspect.signature(websockets.connect).parameters)
        missing = used_kwargs - params
        assert not missing, (
            f"vendored websockets.connect lacks explicit kwargs used by "
            f"ha-mcp: {sorted(missing)}"
        )

    def test_exception_types_we_catch_exist(self):
        assert issubclass(ConnectionClosed, WebSocketException)
        assert issubclass(InvalidStatus, InvalidHandshake)

    def test_import_chain_behind_connect_is_healthy(self):
        import ha_mcp._vendor.websockets.client
        import ha_mcp._vendor.websockets.http11  # noqa: F401


class TestNoSharedWebsocketsImports:
    """src/ha_mcp must never import the shared websockets again."""

    _IMPORT_RE = re.compile(
        r"^\s*(?:import websockets\b|from websockets(?:\.|\s+import\b))",
        re.MULTILINE,
    )

    def test_no_module_imports_the_shared_copy(self):
        offenders = []
        for path in _SRC.rglob("*.py"):
            if _VENDOR in path.parents:
                continue
            if self._IMPORT_RE.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
        assert not offenders, (
            "these modules import the SHARED websockets instead of "
            f"ha_mcp._vendor.websockets: {offenders} — the shared copy is "
            "unowned inside Home Assistant and can be replaced or torn by "
            "any integration's install (#2135/#2146)"
        )

    def test_pyproject_declares_no_websockets_dependency(self):
        import tomllib

        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name

        pyproject = tomllib.loads(
            (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        for dep in pyproject["project"]["dependencies"]:
            # Canonical-name comparison: PEP 503 names are case-insensitive
            # and may carry extras ("WebSockets[x]>=…" is the same package).
            assert canonicalize_name(Requirement(dep).name) != "websockets", (
                f"pyproject declares {dep!r} — the dependency line makes "
                "ha-mcp a writer to the contested shared copy again; the "
                "vendored copy replaces it"
            )
