"""The E2E session fixture, run against the unit tests' real Home Assistant.

``ha_container_with_fresh_config`` stages a config, builds the Home Assistant
container and waits on its readiness gates. Docker is the one thing replaced:
testcontainers' client answers as the daemon does for a running container whose
port 8123 is published on the port Core's test server listens on, so every gate
asks the real ``hass``. GitHub, where staging fetches the HACS frontend, is
unreachable, as on a machine without network.
"""

from __future__ import annotations

import inspect
import tempfile
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import docker
import pytest
import testcontainers.core.container
from homeassistant.config_entries import ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
from testcontainers.core.config import ConnectionMode, testcontainers_config
from yarl import URL

from tests.src.e2e import _conftest_knobs
from tests.src.e2e import conftest as e2e_conftest

# Every variable the fixture writes for the in-process client.
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

# The integration whose loaded entry the readiness gates count.
_READY = "e2e_ready"

# What selects a lane other than the plain container one, or changes its boot.
_LANE_ENV = (
    e2e_conftest.HAOS_IMAGE_ENV,
    "E2E_BACKEND",
    "E2E_NO_TOOLS_ENTRY",
    "HA_TEST_PORT",
    "GITHUB_TOKEN",
    _conftest_knobs.PIP_CACHE_ENV_VAR,
    _conftest_knobs.READY_TIMEOUT_ENV_VAR,
)


class SessionRequest:
    """The ``request`` the session fixture takes: the test's own ``config``, so
    the container goes into the real stash, and the finalizers it registers."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.finalizers: list[Callable[[], object]] = []

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        self.finalizers.append(finalizer)

    def finalize(self) -> None:
        while self.finalizers:
            self.finalizers.pop()()


@dataclass
class Session:
    """One run of the session fixture, and what Docker was asked for."""

    request: SessionRequest
    # testcontainers' Docker client; ``create`` holds the container's mounts
    # and entrypoint.
    docker: MagicMock
    # The container Docker created; its ``logs`` is the Home Assistant log.
    container: MagicMock
    # Where the fixture's temporary directories go.
    temp_root: Path

    def fixture(self) -> Generator[dict[str, Any]]:
        """The session fixture's generator, as pytest runs it."""
        body = inspect.unwrap(e2e_conftest.ha_container_with_fresh_config)
        generator: Generator[dict[str, Any]] = body(self.request, None)
        return generator

    def create_kwargs(self) -> dict[str, Any]:
        """What the fixture asked Docker to create."""
        kwargs: dict[str, Any] = self.docker.create.call_args.kwargs
        return kwargs

    def entrypoint_script(self) -> str:
        script: str = self.create_kwargs()["entrypoint"][-1]
        return script

    def mounts(self) -> dict[str, str]:
        volumes: dict[str, dict[str, str]] = self.create_kwargs()["volumes"]
        return {host: mount["bind"] for host, mount in volumes.items()}

    def staged_dir(self) -> Path:
        """The config directory the fixture staged."""
        [staged] = self.temp_root.glob("ha_e2e_test_*")
        return staged


def _offline(*args: object, **kwargs: object) -> object:
    raise urllib.error.URLError("no network in a unit test")


def plain_container_session(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    *,
    home_assistant_url: str,
    token: str = "",
) -> Session:
    """The plain container lane, with Home Assistant at ``home_assistant_url``."""
    for name in _LANE_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in _STAGED_ENV:
        # Recorded first, so the fixture's own writes are undone after the test.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv(_conftest_knobs.BASE_HOST_ENV_VAR, "127.0.0.1")
    monkeypatch.setattr(e2e_conftest, "TEST_TOKEN", token)
    monkeypatch.setattr(urllib.request, "urlopen", _offline)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    client = MagicMock(name="docker_client")
    client.get_connection_mode.return_value = ConnectionMode.docker_host
    client.host.return_value = "127.0.0.1"
    client.port.return_value = str(URL(home_assistant_url).port)
    container = client.create.return_value
    container.id = "home-assistant"
    container.status = "running"
    container.logs.return_value = b""
    monkeypatch.setattr(
        testcontainers.core.container, "DockerClient", MagicMock(return_value=client)
    )
    monkeypatch.setattr(testcontainers_config, "ryuk_disabled", True)
    daemon = MagicMock(name="docker_daemon")
    daemon.containers.get.return_value = container
    monkeypatch.setattr(docker, "from_env", MagicMock(return_value=daemon))
    return Session(SessionRequest(request.config), client, container, tmp_path)


class _ReadyFlow(ConfigFlow):
    VERSION = 1


@asynccontextmanager
async def running_home_assistant(hass: HomeAssistant) -> AsyncIterator[None]:
    """What the readiness gates wait for: the config-entries API, every entry
    loaded, and ``sun.sun`` in a known state."""
    assert await async_setup_component(hass, "config", {})
    mock_integration(
        hass, MockModule(_READY, async_setup_entry=AsyncMock(return_value=None))
    )
    mock_platform(hass, f"{_READY}.config_flow", None)
    with mock_config_flow(_READY, _ReadyFlow):
        entry = MockConfigEntry(domain=_READY)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        hass.states.async_set("sun.sun", "above_horizon")
        yield
