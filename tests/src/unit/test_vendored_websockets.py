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
3. Nothing under ``src/ha_mcp`` or ``custom_components/ha_mcp_tools``
   (outside ``_vendor``) imports the shared ``websockets``, and the
   embedded listener loads no uvicorn WebSocket protocol — the regressions
   that would silently re-enter the shared-copy war.
"""

from __future__ import annotations

import ast
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


def _scanned_source_files() -> list[Path]:
    """Every first-party module that could import the shared websockets.

    Both trees matter: ``src/ha_mcp`` is the server, and
    ``custom_components/ha_mcp_tools`` runs INSIDE the Home Assistant
    process — the very environment where the shared copy is contested — so
    scanning only the former would let the component quietly re-enter the
    shared-copy war.
    """
    roots = (_SRC, _REPO_ROOT / "custom_components" / "ha_mcp_tools")
    return [
        path
        for root in roots
        for path in root.rglob("*.py")
        if _VENDOR not in path.parents
    ]


def _connect_kwargs_used_in_source() -> set[str]:
    """Collect the keyword names passed to ``websockets.connect(...)`` calls.

    Derived by AST rather than hand-listed so the API-surface guard cannot
    silently stop covering a kwarg someone adds later.
    """
    used: set[str] = set()
    for path in _scanned_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "connect":
                continue
            # ``websockets.connect(...)`` in any binding spelling.
            target = func.value
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else ""
            )
            if name != "websockets":
                continue
            used.update(kw.arg for kw in node.keywords if kw.arg)
    return used


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
        """Every kwarg our code passes to connect() is an explicit parameter.

        The kwarg set is DERIVED from the real call sites rather than
        hand-listed: a hand-copied literal silently stops covering a kwarg
        the moment someone adds one, which would let exactly the renovate
        bump this guard exists to catch (a vendored release that renamed or
        dropped a parameter) sail through and fail at runtime instead.

        Asserted as EXPLICIT parameters — a ``**kwargs`` catch-all would
        swallow a renamed kwarg and forward it to create_connection, which
        raises TypeError only when a connection is actually attempted.
        ``ssl`` is genuinely one of those forwarded kwargs, so it is exempt.
        """
        used_kwargs = _connect_kwargs_used_in_source() - {"ssl"}
        assert used_kwargs, "no websockets.connect(...) call sites found"
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
        offenders = [
            str(path.relative_to(_REPO_ROOT))
            for path in _scanned_source_files()
            if self._IMPORT_RE.search(path.read_text(encoding="utf-8"))
        ]
        assert not offenders, (
            "these modules import the SHARED websockets instead of "
            f"ha_mcp._vendor.websockets: {offenders} — the shared copy is "
            "unowned inside Home Assistant and can be replaced or torn by "
            "any integration's install (#2135/#2146)"
        )

    def test_embedded_listener_loads_no_websocket_protocol(self):
        """uvicorn must not be told to load a WebSocket protocol.

        uvicorn resolves its ``ws`` class EAGERLY in ``Config.load()``, and
        every value except "none" imports the SHARED ``websockets`` package
        (``websockets-sansio`` -> uvicorn's websockets_sansio_impl). That
        would put the unowned, tearable shared copy back on the embedded
        server's startup path — a torn install would crash the listener
        before our vendored client is ever reached — which the vendoring
        exists to make impossible. The MCP app serves Streamable HTTP and
        registers no WebSocket route, so "none" costs nothing.
        """
        source = (
            _REPO_ROOT / "custom_components" / "ha_mcp_tools" / "embedded_server.py"
        ).read_text(encoding="utf-8")
        ws_settings = re.findall(r"""\bws\s*=\s*["']([^"']+)["']""", source)
        assert ws_settings, "no uvicorn ws= setting found — did the call move?"
        assert set(ws_settings) == {"none"}, (
            f"embedded_server configures uvicorn with ws={ws_settings} — every "
            "value but 'none' eagerly imports the shared websockets package "
            "(#2135/#2146)"
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
