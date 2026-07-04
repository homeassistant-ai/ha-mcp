"""Shared ``sys.modules`` stubs for the in-process embedded-server unit tests.

Home Assistant and ``aiohttp`` are not installed in the unit-test environment,
so the component modules under test — ``embedded_server``, ``mcp_webhook``, and
``embedded_setup`` — cannot import their top-level ``homeassistant.*`` / aiohttp
dependencies. This module installs lightweight fakes for exactly that surface,
extending the ``sys.modules``-stub approach already used by
``test_addon_bootstrap.py`` to the auth / requirements / http / webhook /
issue-registry / aiohttp modules the embedded server and webhook ingress need.

Import this module **before** importing any ``custom_components.ha_mcp_tools.*``
module. Installation is idempotent, so several test files can import it and share
one stable set of fakes (the fakes are bound into the component modules'
namespaces at their first import and must not be swapped afterwards).

The fakes are deliberately small — real exception/base classes where the code
depends on ``except``/``class`` semantics, and attribute-recording stand-ins for
aiohttp responses so tests can assert on status, headers, body, and streamed
chunks without a real event loop.
"""

from __future__ import annotations

import pathlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# The ha_mcp_server integration lives at homeassistant-integration/ha_mcp_server/
# (outside custom_components/, to avoid hijacking the HACS listing), so it is not
# importable as custom_components.*. Put that directory on sys.path once so tests
# can ``import ha_mcp_server.<module>`` — the file-path route the webhook-proxy
# tests use for the same out-of-tree situation.
_INTEGRATION_DIR = (
    pathlib.Path(__file__).resolve().parents[3] / "homeassistant-integration"
)

# ---------------------------------------------------------------------------
# Real classes the component code depends on structurally
# ---------------------------------------------------------------------------


class RequirementsNotFound(Exception):
    """Stand-in for ``homeassistant.requirements.RequirementsNotFound``."""

    def __init__(self, domain: str = "ha_mcp_tools", requirements: Any = None) -> None:
        super().__init__(f"{domain}: {requirements}")
        self.domain = domain
        self.requirements = requirements


class HomeAssistantView:
    """Subclassable stand-in for ``homeassistant.components.http`` view base."""

    requires_auth = True
    cors_allowed = False
    url: str | None = None
    name: str | None = None


class IssueSeverity:
    """Stand-in for ``homeassistant.helpers.issue_registry.IssueSeverity``."""

    ERROR = "error"
    WARNING = "warning"
    CRITICAL = "critical"


class ClientError(Exception):
    """Stand-in for ``aiohttp.ClientError`` (must be a real Exception)."""


GROUP_ID_ADMIN = "system-admin"
TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN = "long_lived_access_token"


# ---------------------------------------------------------------------------
# aiohttp web response fakes (attribute recorders)
# ---------------------------------------------------------------------------


class FakeResponse:
    """Records the args ``web.Response(...)`` was built with."""

    def __init__(
        self,
        *,
        status: int = 200,
        text: str | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.text = text
        self.body = body
        self.headers = dict(headers or {})
        self.json_body: Any = None


class FakeStreamResponse:
    """Records prepare / write / write_eof for the SSE streaming branch."""

    def __init__(
        self, *, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.prepared = False
        self.prepared_with: Any = None
        self.written: list[bytes] = []
        self.eof = False

    async def prepare(self, request: Any) -> None:
        self.prepared = True
        self.prepared_with = request

    async def write(self, chunk: bytes) -> None:
        self.written.append(chunk)

    async def write_eof(self, data: bytes = b"") -> None:
        self.eof = True


def fake_json_response(
    obj: Any, *, status: int = 200, headers: dict[str, str] | None = None
) -> FakeResponse:
    resp = FakeResponse(status=status, headers=headers)
    resp.json_body = obj
    resp.headers.setdefault("Content-Type", "application/json")
    return resp


def _make_fake_web() -> SimpleNamespace:
    return SimpleNamespace(
        Request=MagicMock(name="web.Request"),
        Response=FakeResponse,
        StreamResponse=FakeStreamResponse,
        json_response=fake_json_response,
    )


def _make_fake_aiohttp() -> ModuleType:
    mod = ModuleType("aiohttp")
    mod.ClientError = ClientError  # type: ignore[attr-defined]
    mod.ClientTimeout = MagicMock(name="ClientTimeout")  # type: ignore[attr-defined]
    mod.ClientSession = MagicMock(name="ClientSession")  # type: ignore[attr-defined]
    mod.web = _make_fake_web()  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Installation (idempotent)
# ---------------------------------------------------------------------------

_INSTALLED = False


def install() -> None:
    """Install the stub modules into ``sys.modules`` once."""
    global _INSTALLED
    if _INSTALLED:
        return

    if str(_INTEGRATION_DIR) not in sys.path:
        sys.path.insert(0, str(_INTEGRATION_DIR))

    def setmod(name: str, **attrs: Any) -> ModuleType:
        mod = ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    # Generic MagicMock modules — only if a peer test hasn't already stubbed
    # them, so we never clobber a shape another module set up (e.g. the
    # config-flow test's homeassistant.core with a real ``callback``).
    for generic in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.persistent_notification",
        "homeassistant.config",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.config_validation",
        "homeassistant.helpers.storage",
        "homeassistant.loader",
    ):
        sys.modules.setdefault(generic, MagicMock())

    # Specific-shape modules the embedded chain imports. Not stubbed by any
    # other unit-test module, so a direct assignment is safe.
    setmod("homeassistant.auth", const=None, models=None)
    setmod("homeassistant.auth.const", GROUP_ID_ADMIN=GROUP_ID_ADMIN)
    setmod(
        "homeassistant.auth.models",
        TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
    )
    setmod(
        "homeassistant.requirements",
        RequirementsNotFound=RequirementsNotFound,
        async_process_requirements=AsyncMock(name="async_process_requirements"),
        pip_kwargs=MagicMock(name="pip_kwargs", return_value={}),
    )
    setmod("homeassistant.util", package=None)
    setmod(
        "homeassistant.util.package",
        install_package=MagicMock(name="install_package", return_value=True),
    )
    setmod("homeassistant.components.http", HomeAssistantView=HomeAssistantView)
    setmod(
        "homeassistant.components.webhook",
        async_register=MagicMock(name="async_register"),
        async_unregister=MagicMock(name="async_unregister"),
    )
    setmod(
        "homeassistant.helpers.issue_registry",
        async_create_issue=MagicMock(name="async_create_issue"),
        async_delete_issue=MagicMock(name="async_delete_issue"),
        IssueSeverity=IssueSeverity,
    )
    setmod(
        "homeassistant.setup",
        async_setup_component=AsyncMock(
            name="async_setup_component", return_value=True
        ),
    )

    aiohttp_mod = _make_fake_aiohttp()
    sys.modules["aiohttp"] = aiohttp_mod
    sys.modules["aiohttp.web"] = aiohttp_mod.web  # type: ignore[attr-defined]

    _INSTALLED = True


install()


# ---------------------------------------------------------------------------
# Test helpers for the webhook forwarding handler
# ---------------------------------------------------------------------------


def make_request(
    *,
    headers: dict[str, str] | None = None,
    method: str = "POST",
    body: bytes = b"",
    scheme: str = "https",
) -> MagicMock:
    """Build a fake aiohttp request with a plain-dict headers mapping."""
    req = MagicMock(name="Request")
    req.headers = dict(headers or {})
    req.method = method
    req.scheme = scheme
    req.read = AsyncMock(return_value=body)
    return req


class FakeUpstream:
    """Fake upstream response returned by the forwarding session."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self._chunks = chunks or []
        self.content = SimpleNamespace(iter_any=self._iter_any)

    async def read(self) -> bytes:
        return self._body

    async def _iter_any(self):
        for chunk in self._chunks:
            yield chunk


class _UpstreamCtx:
    def __init__(
        self, upstream: FakeUpstream | None, exc: BaseException | None
    ) -> None:
        self._upstream = upstream
        self._exc = exc

    async def __aenter__(self) -> FakeUpstream:
        if self._exc is not None:
            raise self._exc
        assert self._upstream is not None
        return self._upstream

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeSession:
    """Fake aiohttp ClientSession recording the forwarded request args."""

    def __init__(
        self,
        *,
        upstream: FakeUpstream | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._upstream = upstream
        self._exc = exc
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def request(self, **kwargs: Any) -> _UpstreamCtx:
        self.calls.append(kwargs)
        return _UpstreamCtx(self._upstream, self._exc)

    async def close(self) -> None:
        self.closed = True
