"""Shared ``sys.modules`` stubs for the in-process embedded-server unit tests.

Home Assistant and ``aiohttp`` are not installed in the unit-test environment,
so the component modules under test — ``embedded_server``, ``mcp_webhook``, and
``embedded_setup`` — cannot import their top-level ``homeassistant.*`` / aiohttp
dependencies. This module installs lightweight fakes for exactly that surface,
installing ``sys.modules`` fakes for the auth / requirements / http /
webhook / issue-registry / aiohttp modules the embedded server and webhook
ingress need.

Import this module **before** importing any ``custom_components.ha_mcp_tools.*``
embedded module (``embedded_server`` / ``mcp_webhook`` / ``embedded_setup`` /
``embedded_entry``). Installation is idempotent, so several test files can import
it and share one stable set of fakes (the fakes are bound into the component
modules' namespaces at their first import and must not be swapped afterwards).

The fakes are deliberately small — real exception/base classes where the code
depends on ``except``/``class`` semantics, and attribute-recording stand-ins for
aiohttp responses so tests can assert on status, headers, body, and streamed
chunks without a real event loop.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# The embedded server modules now live inside the ha_mcp_tools component
# (``custom_components/ha_mcp_tools/``), importable as
# ``custom_components.ha_mcp_tools.*`` exactly like the rest of the component's
# unit suites — no extra sys.path entry needed.

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


class AwesomeVersion:
    """Minimal stand-in for ``awesomeversion.AwesomeVersion``.

    Compares by numeric release segments — enough for the clean semantic
    versions the embedded-setup tests use (``7.9.0`` vs ``7.10.0``, ``0.11.0``
    vs ``0.14.0``); pre/dev suffixes are ignored. ``__init__`` accepts a str or
    another ``AwesomeVersion`` so the component code's ``AwesomeVersion(a) >
    AwesomeVersion(b)`` works unchanged.
    """

    def __init__(self, version: Any) -> None:
        self._version = str(version)

    def _key(self) -> tuple[int, ...]:
        import re

        return tuple(int(n) for n in re.findall(r"\d+", self._version))

    def __eq__(self, other: Any) -> bool:
        return self._key() == AwesomeVersion(other)._key()

    def __lt__(self, other: Any) -> bool:
        return self._key() < AwesomeVersion(other)._key()

    def __gt__(self, other: Any) -> bool:
        return self._key() > AwesomeVersion(other)._key()

    def __le__(self, other: Any) -> bool:
        return self._key() <= AwesomeVersion(other)._key()

    def __ge__(self, other: Any) -> bool:
        return self._key() >= AwesomeVersion(other)._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        return self._version


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
        content_type: str | None = None,
        charset: str | None = None,
    ) -> None:
        self.status = status
        self.text = text
        self.body = body
        self.headers = dict(headers or {})
        self.content_type = content_type
        self.charset = charset
        self.json_body: Any = None
        self.cookies: dict[str, dict[str, Any]] = {}

    def set_cookie(self, name: str, value: str, **attrs: Any) -> None:
        """Record a Set-Cookie the way ``web.Response.set_cookie`` would send it."""
        self.cookies[name] = {"value": value, **attrs}


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
    # frontend / panel_custom fakes for the settings-UI panel. Registration state
    # is kept in ``hass.data`` so it is isolated per test (each test builds a
    # fresh hass), unlike a module-level registry that would leak across tests.
    _FAKE_PANELS_KEY = "_fake_frontend_panels"

    def _panels(hass: Any) -> dict[str, Any]:
        return hass.data.setdefault(_FAKE_PANELS_KEY, {})

    def _async_panel_exists(hass: Any, frontend_url_path: str) -> bool:
        return frontend_url_path in _panels(hass)

    def _async_remove_panel(
        hass: Any, frontend_url_path: str, *, warn_if_unknown: bool = True
    ) -> None:
        _panels(hass).pop(frontend_url_path, None)

    async def _async_register_panel(
        hass: Any, *, frontend_url_path: str, **kwargs: Any
    ) -> None:
        _panels(hass)[frontend_url_path] = dict(kwargs)

    setmod(
        "homeassistant.components.frontend",
        async_panel_exists=_async_panel_exists,
        async_remove_panel=_async_remove_panel,
        async_register_built_in_panel=MagicMock(name="async_register_built_in_panel"),
    )
    setmod(
        "homeassistant.components.panel_custom",
        async_register_panel=_async_register_panel,
    )

    # Selector stubs for the options-flow dropdowns. Inert pass-through:
    # unit tests hand user_input straight to the flow handler, so the
    # selector never validates; it only needs to construct.
    class _SelectSelectorConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class _SelectSelector:
        def __init__(self, config: Any = None) -> None:
            self.config = config

        def __call__(self, value: Any) -> Any:
            return value

    class _SelectSelectorMode:
        DROPDOWN = "dropdown"
        LIST = "list"

    setmod(
        "homeassistant.helpers.selector",
        SelectSelector=_SelectSelector,
        SelectSelectorConfig=_SelectSelectorConfig,
        SelectSelectorMode=_SelectSelectorMode,
    )
    setmod(
        "homeassistant.helpers.issue_registry",
        async_create_issue=MagicMock(name="async_create_issue"),
        async_delete_issue=MagicMock(name="async_delete_issue"),
        IssueSeverity=IssueSeverity,
    )
    # aiohttp_client + event helpers for the periodic auto-update check
    # (embedded_setup fetches PyPI; embedded_entry registers the interval).
    setmod(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=MagicMock(name="async_get_clientsession"),
    )
    setmod(
        "homeassistant.helpers.event",
        async_track_time_interval=MagicMock(
            name="async_track_time_interval",
            return_value=MagicMock(name="cancel_interval"),
        ),
    )
    # awesomeversion (bundled with HA at runtime) for version comparison in the
    # auto-update and component-compat checks.
    setmod("awesomeversion", AwesomeVersion=AwesomeVersion)
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
        stream_exc: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self._chunks = chunks or []
        self._stream_exc = stream_exc
        self.content = SimpleNamespace(iter_any=self._iter_any)

    async def read(self) -> bytes:
        return self._body

    async def _iter_any(self):
        for chunk in self._chunks:
            yield chunk
        if self._stream_exc is not None:
            raise self._stream_exc


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
