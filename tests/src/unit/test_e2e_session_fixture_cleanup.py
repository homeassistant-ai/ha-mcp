"""A failed testcontainer setup must not leave the staged config on disk.

``_prepare_testcontainer_config`` copies the seeded state into a temp dir and,
when ``GITHUB_TOKEN`` is set, injects it into the HACS config entry there. The
session fixture used to remove that dir only in the post-``yield`` ``finally``,
so any failure before the yield — an unreachable base host, a readiness
timeout, a boot error — skipped cleanup and left the token behind. The cleanup
is registered as a request finalizer instead, which pytest runs on setup
failure too.

Driving the real fixture needs Docker, so the container build, the config
staging, and the readiness gate are replaced with local doubles. What is under
test here is only the registration: the finalizer must exist before the
readiness gate runs.
"""

from __future__ import annotations

from typing import Any

import pytest

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


class _FakeConfig:
    def __init__(self) -> None:
        self.stash: dict[Any, Any] = {}


class _FakeRequest:
    """Records finalizers the way pytest's ``FixtureRequest`` stores them."""

    def __init__(self) -> None:
        self.config = _FakeConfig()
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


def test_a_readiness_failure_still_removes_the_staged_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    staged = tmp_path / "ha_e2e_test_staged"
    (staged / ".storage").mkdir(parents=True)
    tokenless = staged / ".storage" / "core.config_entries"
    tokenless.write_text('{"data": {"token": "injected"}}')

    monkeypatch.setattr(
        e2e_conftest,
        "_prepare_testcontainer_config",
        lambda embedded: (staged, str(staged), {}, None),
    )
    monkeypatch.setattr(
        e2e_conftest, "_build_ha_testcontainer", lambda *args: _FakeContainer()
    )
    monkeypatch.setattr(e2e_conftest, "_is_embedded_backend_selected", lambda: False)
    monkeypatch.setattr(e2e_conftest, "is_haos_backend_selected", lambda: False)
    monkeypatch.setattr(e2e_conftest, "_reset_ha_in_process_caches", lambda: None)

    def unreachable(*args: Any) -> None:
        raise RuntimeError("Home Assistant API did not become ready")

    monkeypatch.setattr(e2e_conftest, "_wait_for_testcontainer_ready", unreachable)

    docker_sdk = pytest.importorskip("docker")

    class _FakeDaemon:
        def ping(self) -> bool:
            return True

    monkeypatch.setattr(docker_sdk, "from_env", _FakeDaemon)
    for name in _STAGED_ENV:
        monkeypatch.setenv(name, "")

    request = _FakeRequest()
    fixture = e2e_conftest.ha_container_with_fresh_config.__wrapped__
    generator = fixture(request, None)
    with pytest.raises(RuntimeError, match="did not become ready"):
        next(generator)

    assert staged.is_dir(), "precondition: staging created the directory"
    assert request.finalizers, "cleanup must be registered before the readiness gate"
    for finalizer in request.finalizers:
        finalizer()
    assert not staged.exists(), "the finalizer must remove the staged config"
