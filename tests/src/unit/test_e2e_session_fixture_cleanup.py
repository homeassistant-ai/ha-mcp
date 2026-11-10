"""A failed testcontainer setup must not leave the staged config on disk.

The staged config is seeded from ``initial_test_state`` and, when
``GITHUB_TOKEN`` is set, that token is written into its HACS config entry. The
session fixture used to remove the directory only in the post-``yield``
``finally``, so any failure before the yield — a staging step, an unreachable
base host, a readiness timeout, a boot error — skipped cleanup and left the
token behind. The fixture now creates the directory and registers its removal
as a request finalizer before staging starts; pytest runs finalizers on setup
failure too.

Driving the real fixture needs Docker, so the container build, the readiness
gate, and (in the readiness case) staging are replaced with local doubles. The
staging case runs the real staging function and fails it partway, which is the
order the finalizer has to survive.

The same doubles pin the URL the fixture hands the in-process client, so a
regression that stops reading ``HA_E2E_BASE_HOST`` fails here rather than only
in the resolver unit test.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.src.e2e import _conftest_testcontainer
from tests.src.e2e import conftest as e2e_conftest

# Every variable the fixture writes for the in-process client; monkeypatch
# records the pre-test values so the test cannot leak them into the session.
_STAGED_ENV = (
    "HOMEASSISTANT_URL",
    "HOMEASSISTANT_TOKEN",
    "ENABLE_BETA_FEATURES",
    "ENABLE_YAML_CONFIG_EDITING",
    "ENABLE_YAML_PACKAGES_AUTOMATION",
    "ENABLE_YAML_PACKAGES_SCRIPT",
    "ENABLE_YAML_PACKAGES_SCENE",
    "HAMCP_ENABLE_FILESYSTEM_TOOLS",
    "HA_MCP_EXTRA_YAML_KEYS",
    "ENABLE_STRICT_MANDATORY_BPS",
    "ENABLE_SNAPSHOT_DELETE",
)


class _RequestConfigView:
    """The ``request.config`` surface the fixture touches: ``stash`` only."""

    def __init__(self) -> None:
        self.stash: dict[Any, Any] = {}


class _FakeRequest:
    """Records finalizers the way pytest's ``FixtureRequest`` stores them."""

    def __init__(self) -> None:
        self.config = _RequestConfigView()
        self.finalizers: list[Any] = []

    def addfinalizer(self, func: Any) -> None:
        self.finalizers.append(func)


class _FakeContainer:
    """The slice of ``DockerContainer`` the fixture touches before readiness."""

    def __enter__(self) -> _FakeContainer:
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False

    def get_exposed_port(self, port: int) -> int:
        return 32768

    def get_container_host_ip(self) -> str:
        return "localhost"


class _FakeDaemon:
    def ping(self) -> bool:
        return True


def _fixture_world(
    monkeypatch: pytest.MonkeyPatch, prepare: Any, readiness: Any
) -> _FakeRequest:
    """Patch everything the fixture touches except its own behaviour.

    The settings/WebSocket reset stays real: it is what makes the fixture's URL
    reach settings-backed clients, and one test asserts exactly that.
    """
    monkeypatch.setattr(e2e_conftest, "_prepare_testcontainer_config", prepare)
    monkeypatch.setattr(
        e2e_conftest, "_build_ha_testcontainer", lambda *args: _FakeContainer()
    )
    monkeypatch.setattr(e2e_conftest, "_is_embedded_backend_selected", lambda: False)
    monkeypatch.setattr(e2e_conftest, "is_haos_backend_selected", lambda: False)
    monkeypatch.setattr(e2e_conftest, "_wait_for_testcontainer_ready", readiness)
    docker_sdk = pytest.importorskip("docker")
    monkeypatch.setattr(docker_sdk, "from_env", _FakeDaemon)
    for name in _STAGED_ENV:
        monkeypatch.setenv(name, "")
    return _FakeRequest()


def _drive_to_failure(request: _FakeRequest, error: str) -> None:
    fixture = e2e_conftest.ha_container_with_fresh_config.__wrapped__
    with pytest.raises(RuntimeError, match=error):
        next(fixture(request, None))


def _assert_finalizer_removed(request: _FakeRequest, staged: Path) -> None:
    assert staged.is_dir(), "precondition: the fixture created the staging directory"
    assert request.finalizers, "cleanup must be registered before staging starts"
    for finalizer in request.finalizers:
        finalizer()
    assert not staged.exists(), "the finalizer must remove the staged config"


def test_a_staging_failure_still_removes_the_staged_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_prepare = _conftest_testcontainer._prepare_testcontainer_config
    seen: dict[str, Path] = {}

    def recording_prepare(embedded: bool, config_path: Path) -> Any:
        seen["config_path"] = config_path
        return real_prepare(embedded, config_path)

    def staging_fails(initial_state_path: Path) -> None:
        raise RuntimeError("HACS frontend download failed")

    monkeypatch.setattr(_conftest_testcontainer, "_ensure_hacs_frontend", staging_fails)
    request = _fixture_world(monkeypatch, recording_prepare, lambda *args: None)

    _drive_to_failure(request, "HACS frontend download failed")

    _assert_finalizer_removed(request, Path(seen["config_path"]))


def test_a_failure_after_token_injection_still_removes_the_staged_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leaked directory is the one that matters: it holds an injected token."""
    real_prepare = _conftest_testcontainer._prepare_testcontainer_config
    seen: dict[str, Path] = {}
    token = "ghp_staged_token_must_not_survive"

    def recording_prepare(embedded: bool, config_path: Path) -> Any:
        seen["config_path"] = config_path
        return real_prepare(embedded, config_path)

    def component_install_fails(
        config_path: Path,
        component_src: Path,
        domain: str,
        title: str,
        *,
        seed_entry: bool = True,
    ) -> bool:
        storage = Path(config_path) / ".storage" / "core.config_entries"
        assert token in storage.read_text(), (
            "precondition: the token is injected before this failing step"
        )
        raise RuntimeError(f"{domain} install failed")

    # The HACS frontend download is a 51 MB network step; the token injection
    # this test targets happens after it, so skip just the download.
    monkeypatch.setattr(
        _conftest_testcontainer, "_ensure_hacs_frontend", lambda path: None
    )
    monkeypatch.setattr(
        _conftest_testcontainer, "_install_custom_component", component_install_fails
    )
    monkeypatch.setenv("GITHUB_TOKEN", token)
    request = _fixture_world(monkeypatch, recording_prepare, lambda *args: None)

    _drive_to_failure(request, "install failed")

    _assert_finalizer_removed(request, Path(seen["config_path"]))


def test_a_readiness_failure_still_removes_the_staged_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Path] = {}

    def fake_prepare(embedded: bool, config_path: Path) -> Any:
        seen["config_path"] = config_path
        storage = config_path / ".storage"
        storage.mkdir()
        (storage / "core.config_entries").write_text('{"data": {"token": "x"}}')
        return config_path, {}, None

    def unreachable(*args: Any) -> None:
        raise RuntimeError("Home Assistant API did not become ready")

    request = _fixture_world(monkeypatch, fake_prepare, unreachable)

    _drive_to_failure(request, "Home Assistant API did not become ready")

    _assert_finalizer_removed(request, Path(seen["config_path"]))


def test_a_successful_session_removes_the_staged_config_after_the_container_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Path] = {}

    def fake_prepare(embedded: bool, config_path: Path) -> Any:
        seen["config_path"] = config_path
        (config_path / "configuration.yaml").write_text("default_config:\n")
        return config_path, {}, None

    request = _fixture_world(monkeypatch, fake_prepare, lambda *args: None)
    fixture = e2e_conftest.ha_container_with_fresh_config.__wrapped__
    generator = fixture(request, None)

    next(generator)
    staged = Path(seen["config_path"])
    assert staged.is_dir(), "the session needs its staged config while it runs"
    assert request.finalizers, "cleanup must be registered for a successful session too"

    generator.close()

    assert staged.is_dir(), (
        "the finalizer runs after the container block releases the bind mount; "
        "removing the directory earlier would pull it out from under Home Assistant"
    )
    for finalizer in request.finalizers:
        finalizer()
    assert not staged.exists(), "the finalizer must remove the staged config"


@pytest.mark.parametrize(
    ("override", "expected_host"),
    [(None, "localhost"), ("192.0.2.10", "192.0.2.10")],
)
def test_the_fixture_builds_the_client_url_from_the_resolved_host(
    monkeypatch: pytest.MonkeyPatch,
    override: str | None,
    expected_host: str,
) -> None:
    from types import SimpleNamespace

    from ha_mcp import config
    from ha_mcp.client.websocket_client import websocket_manager

    def fake_prepare(embedded: bool, config_path: Path) -> Any:
        return config_path, {}, None

    request = _fixture_world(monkeypatch, fake_prepare, lambda *args: None)
    if override is None:
        monkeypatch.delenv("HA_E2E_BASE_HOST", raising=False)
    else:
        monkeypatch.setenv("HA_E2E_BASE_HOST", override)

    # A client built before the session must not keep the stale URL: prime the
    # settings cache and the connection pool with one, then let the fixture's
    # real reset replace them. The TLS mode is pinned on both sides so an
    # inherited HA_VERIFY_SSL=false cannot make the post-reset lookup miss the
    # seeded entry and pass without the pool actually being cleared.
    stale_url, stale_token = "http://stale.invalid:1", "stale-token"
    monkeypatch.setenv("HA_VERIFY_SSL", "false")
    stale = SimpleNamespace(homeassistant_url=stale_url, verify_ssl=True)
    monkeypatch.setattr(config, "_settings", stale)
    assert config.get_global_settings() is stale
    # Replace the pool dicts instead of editing and restoring them by hand:
    # Core's plugin gives every test a fresh event loop, so a pool carried into
    # the next test would be bound to a loop that is already closed. The
    # fixture clears whatever ``_clients`` it finds, and monkeypatch restores
    # the original dicts untouched at teardown.
    stale_client = object()
    stale_key = websocket_manager._pool_key(stale_url, stale_token, True)[2]
    monkeypatch.setattr(websocket_manager, "_clients", {stale_key: stale_client})
    monkeypatch.setattr(websocket_manager, "_last_used", {stale_key: 0.0})
    assert websocket_manager.pooled_client(stale_url, stale_token, True) is stale_client

    fixture = e2e_conftest.ha_container_with_fresh_config.__wrapped__
    generator = fixture(request, None)
    try:
        info = next(generator)
        assert info["base_url"] == f"http://{expected_host}:32768"
        assert os.environ["HOMEASSISTANT_URL"] == info["base_url"]
        assert config.get_global_settings().homeassistant_url == info["base_url"]
        # The next lookup must build a client for the session URL rather than
        # reuse the pooled one that points at the stale address.
        assert websocket_manager.pooled_client(stale_url, stale_token, True) is None
    finally:
        generator.close()
        for finalizer in request.finalizers:
            finalizer()
