"""The final same-VM HAOS Core TLS scenario's helpers, against real Core.

``haos_runtime`` stages, reads and promotes Core's HTTP config over Core's
``http/config*`` WebSocket commands. Every reply here is real Core's: the
commands run on the ``hass`` of ``core_client`` and the runtime reaches them
over a real WebSocket.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import pytest
from homeassistant.core import CoreState
from pytest_homeassistant.common import async_mock_service
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus, InvalidURI
from websockets.http11 import Response
from websockets.sync import client as ws_client
from websockets.sync.client import ClientConnection

from tests.src import haos_runtime

from ._haos_build_core import BuildClock

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from ha_mcp.client.rest_client import HomeAssistantClient

pytest_plugins = ["pytester"]

T = TypeVar("T")

PROXY_SETTINGS: dict[str, Any] = {
    "use_x_forwarded_for": True,
    "trusted_proxies": ["172.30.32.0/23"],
    "ip_ban_enabled": True,
}


@dataclass
class Core:
    """Real Core's HTTP config commands, as the runtime reaches them."""

    hass: HomeAssistant
    base_url: str
    token: str
    # Core's restart requests: its ``homeassistant.restart`` service is not run.
    restarts: list[ServiceCall]

    @property
    def settings(self) -> dict[str, Any]:
        """An HTTP config Core accepts, on the port it already serves."""
        return {"server_port": self.hass.http.server_port, **PROXY_SETTINGS}

    async def call(
        self,
        func: Callable[..., T],
        *args: Any,
        token: str | None = None,
        **kwargs: Any,
    ) -> T:
        """Run a runtime function in a worker thread while Core serves it."""
        return await self.hass.async_add_executor_job(
            partial(func, self.base_url, token or self.token, *args, **kwargs)
        )

    async def stable(self) -> dict[str, Any]:
        config = await self.call(haos_runtime.get_home_assistant_http_config)
        return config["stable"]


@pytest.fixture
async def core(hass: HomeAssistant, core_client: HomeAssistantClient) -> Core:
    restarts = async_mock_service(hass, "homeassistant", "restart")
    return Core(hass, core_client.base_url, core_client.token, restarts)


async def test_a_tls_trial_keeps_the_proxy_and_port_settings_of_the_active_config(
    core: Core, tmp_path: Path
) -> None:
    """The trial is built from the config Core reports as stable, so Core takes
    it as a config (its read-only metadata is gone) and still serves the same
    proxy and port settings once TLS is on."""
    await core.call(haos_runtime.configure_home_assistant_http, core.settings)
    await core.call(haos_runtime.promote_home_assistant_http_config)
    stable = await core.stable()
    certificate = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    certificate.write_text("certificate")
    key.write_text("key")

    trial = haos_runtime.build_home_assistant_tls_config(
        stable, certificate_path=str(certificate), key_path=str(key)
    )

    assert await core.call(haos_runtime.configure_home_assistant_http, trial) is True
    pending = (await core.call(haos_runtime.get_home_assistant_http_config))["pending"]
    for setting in (*PROXY_SETTINGS, "server_port"):
        assert pending[setting] == stable[setting]
    assert pending["ssl_certificate"] == str(certificate)
    assert pending["ssl_key"] == str(key)


async def test_configure_home_assistant_http_requests_in_place_core_restart(
    core: Core,
) -> None:
    """The runtime switch stages the config in Core's pending slot and has Core
    restart itself to apply it, rather than restarting Core from outside."""
    restarted = await core.call(
        haos_runtime.configure_home_assistant_http, core.settings
    )
    await core.hass.async_block_till_done()

    config = await core.call(haos_runtime.get_home_assistant_http_config)
    assert restarted is True
    for setting, value in PROXY_SETTINGS.items():
        assert config["pending"][setting] == value
    assert len(core.restarts) == 1


async def test_get_home_assistant_http_config_reads_stable_slot(core: Core) -> None:
    """The runtime switch derives its TLS trial from Core's active store."""
    result = await core.call(haos_runtime.get_home_assistant_http_config)

    assert result["pending"] is None
    assert isinstance(result["stable"], dict)
    assert "server_port" in result["stable"]


async def test_promote_home_assistant_http_config_commits_pending_slot(
    core: Core,
) -> None:
    """The live TLS/HTTP trial is promoted before its auto-revert deadline."""
    await core.call(haos_runtime.configure_home_assistant_http, core.settings)

    await core.call(haos_runtime.promote_home_assistant_http_config)

    config = await core.call(haos_runtime.get_home_assistant_http_config)
    assert config["pending"] is None
    for setting, value in PROXY_SETTINGS.items():
        assert config["stable"][setting] == value


def _status(code: int) -> InvalidStatus:
    return InvalidStatus(Response(code, "status", Headers()))


@pytest.mark.parametrize(
    "transient",
    [
        TimeoutError("timed out while waiting for handshake response"),
        ConnectionRefusedError("connection refused"),
        _status(503),
    ],
    ids=["handshake-timeout", "refused", "proxy-503"],
)
async def test_ws_connect_retries_until_core_accepts_the_handshake(
    core: Core,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    transient: Exception,
) -> None:
    """After a Core restart HTTP answers before the WebSocket handshake does."""
    clock = BuildClock()
    monkeypatch.setattr(haos_runtime, "time", clock)
    real_connect = ws_client.connect
    attempts: list[float] = []

    def connect(*args: Any, **kwargs: Any) -> ClientConnection:
        attempts.append(kwargs["open_timeout"])
        if len(attempts) < 3:
            raise transient
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(ws_client, "connect", connect)
    caplog.set_level("DEBUG", logger=haos_runtime.LOG.name)

    config = await core.call(haos_runtime.get_home_assistant_http_config)

    assert "stable" in config
    assert len(attempts) == 3
    assert len(clock.sleeps) == 2
    assert caplog.text.count("not ready yet") == 2


def test_ws_connect_retry_stops_at_the_command_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Core that never accepts the handshake fails within the caller's budget."""
    clock = BuildClock()
    monkeypatch.setattr(haos_runtime, "time", clock)
    attempts: list[float] = []

    def connect(*args: Any, **kwargs: Any) -> ClientConnection:
        attempts.append(kwargs["open_timeout"])
        clock.advance(kwargs["open_timeout"])
        raise TimeoutError("timed out while waiting for handshake response")

    monkeypatch.setattr(ws_client, "connect", connect)

    with pytest.raises(TimeoutError):
        haos_runtime.promote_home_assistant_http_config(
            "https://127.0.0.1:18123", "token", timeout=100.0, verify_ssl=False
        )

    # Three full 30 s attempts with 2 s pauses, then only the 4 s still left.
    assert attempts == [30.0, 30.0, 30.0, 4.0]
    assert clock.now == 100.0


@pytest.mark.parametrize(
    "permanent",
    [
        _status(403),
        InvalidURI("wss://bad", "not a valid URI"),
        ssl.SSLCertVerificationError("certificate verify failed"),
    ],
    ids=["forbidden", "invalid-uri", "bad-certificate"],
)
def test_ws_connect_fails_at_once_on_a_permanent_error(
    monkeypatch: pytest.MonkeyPatch, permanent: Exception
) -> None:
    """A rejected status, URL or certificate never clears up by waiting."""
    clock = BuildClock()
    monkeypatch.setattr(haos_runtime, "time", clock)
    attempts: list[float] = []

    def connect(*args: Any, **kwargs: Any) -> ClientConnection:
        attempts.append(kwargs["open_timeout"])
        raise permanent

    monkeypatch.setattr(ws_client, "connect", connect)

    with pytest.raises(type(permanent)):
        haos_runtime.promote_home_assistant_http_config(
            "https://127.0.0.1:18123", "token", verify_ssl=False
        )

    assert len(attempts) == 1
    assert clock.sleeps == []


async def test_ws_command_shares_one_timeout_across_connect_and_reply(
    core: Core, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow handshake leaves the reply wait only what remains of ``timeout``."""
    clock = BuildClock()
    monkeypatch.setattr(haos_runtime, "time", clock)
    real_connect = ws_client.connect
    real_recv = ClientConnection.recv
    received = 0

    def slow_connect(*args: Any, **kwargs: Any) -> ClientConnection:
        clock.advance(40.0)
        return real_connect(*args, **kwargs)

    def recv(self: ClientConnection, *args: Any, **kwargs: Any) -> Any:
        nonlocal received
        received += 1
        if received <= 2:  # Core's own auth_required and auth_ok
            return real_recv(self, *args, **kwargs)
        # Core's answer to the command never reaches the client.
        clock.advance(kwargs["timeout"])
        raise TimeoutError

    monkeypatch.setattr(ws_client, "connect", slow_connect)
    monkeypatch.setattr(ClientConnection, "recv", recv)

    with pytest.raises(TimeoutError, match="got no response"):
        await core.call(
            haos_runtime.get_home_assistant_http_config, timeout=60.0, verify_ssl=False
        )

    assert clock.now == 60.0


# Core closes a socket whose token it refused; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket, which nothing
# cancels (as in test_backup_auth_refusal.py).
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_ws_command_rejected_auth_fails_loudly(core: Core) -> None:
    """A login Core refuses never falls through to the command send."""
    with pytest.raises(RuntimeError, match="auth rejected"):
        await core.call(
            haos_runtime.get_home_assistant_http_config, token="not-a-token"
        )


async def test_ws_command_unsuccessful_result_fails_loudly(core: Core) -> None:
    """Core refusing the command raises with Core's error code."""
    core.hass.set_state(CoreState.starting)
    try:
        with pytest.raises(RuntimeError, match="not_running"):
            await core.call(haos_runtime.configure_home_assistant_http, core.settings)
    finally:
        core.hass.set_state(CoreState.running)


def test_tls_module_contains_exactly_one_test() -> None:
    """The end-of-queue scheduling guarantee needs a one-test module.

    ``loadscope`` schedules whole modules; a second test here would make the
    TLS scope larger than the other one-test scopes and let the reorder sort
    schedule the destructive Core restart mid-suite.
    """
    import ast

    module = (
        Path(__file__).parents[1] / "e2e" / "haos_only" / "test_zz_manage_app_tls.py"
    )
    tree = ast.parse(module.read_text())
    test_defs = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    ]
    assert test_defs == ["test_manage_app_reproduces_legacy_tls_failure_then_uses_fix"]


_COLLECTION_ENV = (
    "HAOS_TEST_IMAGE_PATH",
    "HAOS_TEST_MODE",
    "HAOS_EXPECTED_OS_VERSION",
    "HAOS_EXPECTED_SUPERVISOR_CHANNEL",
    "HAOS_EXPECTED_SUPERVISOR_MIN_VERSION",
    "HAOS_EXPECTED_CORE_VERSION",
    "E2E_BACKEND",
    "E2E_NO_TOOLS_ENTRY",
)


def _collect_e2e_tests(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> list[tuple[str, str]]:
    """Run the E2E suite's collection hook over a TLS, a beta and two ordinary
    tests, defined in that order; return (test, outcome) in the order run."""
    for name in _COLLECTION_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    pytester.makeconftest(
        "from tests.src.e2e._conftest_collection import pytest_collection_modifyitems"
    )
    pytester.makeini(
        "[pytest]\nmarkers =\n    haos_tls: the TLS scenario\n"
        "    beta_haos_only: beta lanes only\n"
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.haos_tls
        def test_tls():
            pass

        @pytest.mark.beta_haos_only
        def test_beta():
            pass

        def test_first():
            pass

        def test_last():
            pass
        """
    )
    # Only pytest and this conftest: the plugins installed for the unit tests
    # (Core's among them) are not the E2E suite's.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    result = pytester.runpytest("-p", "no:cacheprovider", "-v")

    return [
        (words[0], words[1])
        for line in result.outlines
        if "::" in line and (words := line.split("::", 1)[1].split())
    ]


def test_the_tls_scenario_is_the_last_test_of_the_e2e_queue(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook reorders collection so the haos_tls test is last.

    Core restarts only after a worker's ordinary work has run."""
    outcomes = _collect_e2e_tests(pytester, monkeypatch, {})

    assert [name for name, _ in outcomes] == [
        "test_beta",
        "test_first",
        "test_last",
        "test_tls",
    ]


@pytest.mark.parametrize(
    ("haos_embedded", "beta"),
    [(False, False), (True, False), (False, True)],
    ids=["ordinary-lane", "haos-embedded-lane", "beta-haos-lane"],
)
def test_gated_e2e_tests_skip_exactly_where_their_lane_is_off(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    haos_embedded: bool,
    beta: bool,
) -> None:
    """The TLS scenario runs only on the embedded HAOS lane and the runtime
    version canary only where a beta image is expected. An inverted condition
    would silently stop them running anywhere, under the skip ceilings' radar,
    and an ungated test runs on every lane."""
    env: dict[str, str] = {}
    if haos_embedded:
        image = tmp_path / "image.qcow2"
        image.write_bytes(b"")
        env = {"HAOS_TEST_IMAGE_PATH": str(image), "HAOS_TEST_MODE": "embedded"}
    if beta:
        env["HAOS_EXPECTED_OS_VERSION"] = "17.0"

    outcomes = dict(_collect_e2e_tests(pytester, monkeypatch, env))

    assert outcomes == {
        "test_tls": "PASSED" if haos_embedded else "SKIPPED",
        "test_beta": "PASSED" if beta else "SKIPPED",
        "test_first": "PASSED",
        "test_last": "PASSED",
    }
