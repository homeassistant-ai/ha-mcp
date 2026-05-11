"""Unit tests for the shared Supervisor httpx client factory.

Covers :func:`ha_mcp.client.supervisor_client.make_supervisor_httpx_client`,
which builds the ``httpx.AsyncClient`` instances used everywhere ha-mcp
talks directly to the Home Assistant Supervisor REST API.

The factory itself is a one-liner around ``httpx.AsyncClient`` — these tests
guard the contract callers rely on:

- ``base_url`` resolved through :func:`ha_mcp._version.get_supervisor_base_url`
  (so the ``SUPERVISOR_BASE_URL`` E2E override flows through)
- ``Authorization: Bearer ${SUPERVISOR_TOKEN}`` preset on the client at
  construction time; per-call headers layer on top without displacing it
- ``timeout`` / ``verify`` forwarded verbatim, both as plain ``float`` and as
  :class:`httpx.Timeout`
- Absent or empty ``SUPERVISOR_TOKEN`` raises ``RuntimeError`` at construction
  time rather than emitting a malformed ``Authorization: Bearer `` header
  that Supervisor would reject as a bad token
"""

from unittest.mock import patch

import httpx
import pytest

from ha_mcp.client.supervisor_client import make_supervisor_httpx_client


@pytest.fixture
def supervisor_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a non-empty ``SUPERVISOR_TOKEN`` for the duration of one test."""
    token = "test-supervisor-token-abc123"
    monkeypatch.setenv("SUPERVISOR_TOKEN", token)
    return token


@pytest.fixture
def no_supervisor_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``SUPERVISOR_TOKEN`` is unset for the duration of one test."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)


@pytest.fixture
def no_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no ``SUPERVISOR_BASE_URL`` override leaks in from the host env."""
    monkeypatch.delenv("SUPERVISOR_BASE_URL", raising=False)


@pytest.mark.asyncio
async def test_returns_async_client(
    supervisor_token: str, no_base_url_override: None
) -> None:
    """Factory returns a ready-to-use ``httpx.AsyncClient``."""
    async with make_supervisor_httpx_client(timeout=5.0, verify=True) as client:
        assert isinstance(client, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_base_url_defaults_to_supervisor(
    supervisor_token: str, no_base_url_override: None
) -> None:
    """Default base URL is the in-addon ``http://supervisor`` hostname."""
    async with make_supervisor_httpx_client(timeout=5.0, verify=True) as client:
        assert str(client.base_url) == "http://supervisor"


@pytest.mark.asyncio
async def test_base_url_honors_env_override(
    supervisor_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SUPERVISOR_BASE_URL`` env override flows through to the client.

    This is the path E2E tests use to point the call sites at a local mock
    without /etc/hosts hacks — guarding it here keeps that wiring intact.
    """
    monkeypatch.setenv("SUPERVISOR_BASE_URL", "http://127.0.0.1:9876")
    async with make_supervisor_httpx_client(timeout=5.0, verify=True) as client:
        assert str(client.base_url) == "http://127.0.0.1:9876"


@pytest.mark.asyncio
async def test_authorization_header_uses_token_from_env(
    supervisor_token: str, no_base_url_override: None
) -> None:
    """``Authorization`` header is ``Bearer <SUPERVISOR_TOKEN>``."""
    async with make_supervisor_httpx_client(timeout=5.0, verify=True) as client:
        assert client.headers["Authorization"] == f"Bearer {supervisor_token}"


def test_raises_on_absent_token(no_supervisor_token: None) -> None:
    """Missing ``SUPERVISOR_TOKEN`` raises ``RuntimeError`` at construction.

    Each call site short-circuits before reaching the factory when the
    token is absent; this test pins the factory's defensive fast-fail for
    any future direct caller that forgets to guard. A malformed
    ``Authorization: Bearer `` header would otherwise be read by
    Supervisor as a token rejection (401), masking the missing-env-var
    root cause.
    """
    with pytest.raises(RuntimeError, match="SUPERVISOR_TOKEN"):
        make_supervisor_httpx_client(timeout=5.0, verify=True)


def test_raises_on_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty-string ``SUPERVISOR_TOKEN`` is treated the same as absent.

    Some environments set the variable to an empty string rather than
    unsetting it; both must short-circuit so the factory never emits a
    malformed header.
    """
    monkeypatch.setenv("SUPERVISOR_TOKEN", "")
    with pytest.raises(RuntimeError, match="SUPERVISOR_TOKEN"):
        make_supervisor_httpx_client(timeout=5.0, verify=True)


@pytest.mark.asyncio
async def test_timeout_forwarded_as_float(
    supervisor_token: str, no_base_url_override: None
) -> None:
    """A plain ``float`` timeout is forwarded to the underlying client.

    httpx normalises ``float`` to a :class:`httpx.Timeout` with the same
    value applied to all four phases — assert connect + read carry the
    value through.
    """
    async with make_supervisor_httpx_client(timeout=7.5, verify=True) as client:
        assert client.timeout.connect == 7.5
        assert client.timeout.read == 7.5


@pytest.mark.asyncio
async def test_timeout_forwarded_as_httpx_timeout(
    supervisor_token: str, no_base_url_override: None
) -> None:
    """An :class:`httpx.Timeout` instance is forwarded verbatim.

    ``rest_client._supervisor_logs_get`` passes ``httpx.Timeout(self.timeout)``
    rather than a bare float; this test guards that the helper accepts the
    rich type unchanged.
    """
    timeout = httpx.Timeout(connect=2.0, read=30.0, write=30.0, pool=30.0)
    async with make_supervisor_httpx_client(timeout=timeout, verify=True) as client:
        assert client.timeout.connect == 2.0
        assert client.timeout.read == 30.0


def test_verify_forwarded_to_async_client_kwarg(supervisor_token: str) -> None:
    """``verify`` is forwarded verbatim to the underlying ``httpx.AsyncClient``.

    httpx does not expose the constructor's ``verify`` value via any public
    attribute on the client, so a real-instance assertion can't tell apart
    ``verify=True`` from ``verify=False``. Patch the constructor and check
    the kwarg directly — this is the only way to guard against a regression
    that drops the parameter on the floor while still returning a valid
    client. The ``verify`` flag is a no-op for plain ``http://supervisor``
    today, but call sites pass it for symmetry with the HTTPS-override
    path and that contract has to hold.
    """
    with patch("ha_mcp.client.supervisor_client.httpx.AsyncClient") as mock_ctor:
        make_supervisor_httpx_client(timeout=5.0, verify=False)
        assert mock_ctor.call_args.kwargs["verify"] is False

        mock_ctor.reset_mock()
        make_supervisor_httpx_client(timeout=5.0, verify=True)
        assert mock_ctor.call_args.kwargs["verify"] is True


@pytest.mark.asyncio
async def test_per_call_headers_layer_over_ctor_authorization(
    supervisor_token: str, no_base_url_override: None
) -> None:
    """Per-call headers (e.g. ``Accept``) layer on top of the ctor-set
    ``Authorization`` rather than displacing it.

    ``_supervisor_logs_get`` passes ``headers={"Accept": "text/plain"}`` on
    the call while the factory presets ``Authorization`` at the ctor.
    httpx merges request-level headers with client-level headers; this
    test pins that contract so a regression that moved ``Authorization``
    to per-call defaults — which would silently drop it whenever a caller
    supplies its own ``headers=`` kwarg — is caught.
    """
    async with make_supervisor_httpx_client(timeout=5.0, verify=True) as client:
        request = client.build_request(
            "GET", "/addons/self/logs", headers={"Accept": "text/plain"}
        )
        assert request.headers["Authorization"] == f"Bearer {supervisor_token}"
        assert request.headers["Accept"] == "text/plain"
