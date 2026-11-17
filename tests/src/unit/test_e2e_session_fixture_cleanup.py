"""A failed testcontainer setup must not leave the staged config on disk.

The staged config is seeded from ``initial_test_state`` and, when
``GITHUB_TOKEN`` is set, that token is written into its HACS config entry. The
session fixture used to remove the directory only in the post-``yield``
``finally``, so any failure before the yield — a staging step, an unreachable
base host, a readiness timeout, a boot error — skipped cleanup and left the
token behind. The fixture now creates the directory and registers its removal
as a request finalizer before staging starts; pytest runs finalizers on setup
failure too.

Each test runs the real fixture (``_e2e_session``): real staging, the real
container build, and readiness gates that ask the unit tests' real Home
Assistant. Only Docker is replaced.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager
from tests.src.e2e import _conftest_knobs

from ._core_client import refused_url
from ._e2e_session import (
    SessionRequest,
    plain_container_session,
    running_home_assistant,
)


def _assert_finalizer_removed(request: SessionRequest, staged: Path) -> None:
    assert staged.is_dir(), "precondition: the fixture created the staging directory"
    assert request.finalizers, "cleanup must be registered before staging starts"
    request.finalize()
    assert not staged.exists(), "the finalizer must remove the staged config"


def test_a_staging_failure_after_the_token_is_written_still_removes_it(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """The leaked directory is the one that matters: it holds an injected token.

    The embedded lane's wheel build is the last staging step, after the token
    is written; with no ``uv`` on the PATH it fails the way a broken build does.
    """
    token = "ghp_staged_token_must_not_survive"
    # Staging fails before the container starts, so nothing connects to it.
    session = plain_container_session(
        monkeypatch, request, tmp_path, home_assistant_url="http://127.0.0.1:1"
    )
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("E2E_BACKEND", "embedded")
    no_tools = tmp_path / "no-tools"
    no_tools.mkdir()
    monkeypatch.setenv("PATH", str(no_tools))

    with pytest.raises(FileNotFoundError, match="uv"):
        next(session.fixture())

    staged = session.staged_dir()
    storage = staged / ".storage" / "core.config_entries"
    assert token in storage.read_text(), "precondition: the token was written"
    _assert_finalizer_removed(session.request, staged)


def test_a_readiness_failure_still_removes_the_staged_config(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    socket_enabled: None,
) -> None:
    session = plain_container_session(
        monkeypatch, request, tmp_path, home_assistant_url=refused_url()
    )
    monkeypatch.setenv(_conftest_knobs.READY_TIMEOUT_ENV_VAR, "1")

    with pytest.raises(pytest.fail.Exception, match="did not become ready"):
        next(session.fixture())

    _assert_finalizer_removed(session.request, session.staged_dir())


async def test_a_successful_session_removes_the_staged_config_after_the_container_stops(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    session = plain_container_session(
        monkeypatch,
        request,
        tmp_path,
        home_assistant_url=core_client.base_url,
        token=core_client.token,
    )
    generator = session.fixture()

    async with running_home_assistant(hass):
        info = await hass.async_add_executor_job(next, generator)
    staged = Path(info["config_path"])
    assert await hass.async_add_executor_job(staged.is_dir), (
        "the session needs its staged config while it runs"
    )
    assert session.request.finalizers, (
        "cleanup must be registered for a successful session too"
    )

    generator.close()

    assert await hass.async_add_executor_job(staged.is_dir), (
        "the finalizer runs after the container block releases the bind mount; "
        "removing the directory earlier would pull it out from under Home Assistant"
    )
    await hass.async_add_executor_job(session.request.finalize)
    assert not await hass.async_add_executor_job(staged.exists), (
        "the finalizer must remove the staged config"
    )


async def test_the_fixture_points_every_client_at_the_resolved_host(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """``HA_E2E_BASE_HOST`` names the host, and a client built before the
    session must not keep the previous Home Assistant's URL: the settings cache
    and a pooled, connected client both point at it until the fixture resets
    them."""
    session = plain_container_session(
        monkeypatch,
        request,
        tmp_path,
        home_assistant_url=core_client.base_url,
        token=core_client.token,
    )
    previous_url = core_client.base_url.replace("127.0.0.1", "localhost")
    monkeypatch.setenv("HOMEASSISTANT_URL", previous_url)
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", core_client.token)
    config.reset_global_settings()
    assert config.get_global_settings().homeassistant_url == previous_url
    previous = await websocket_manager.get_client(
        core_client.base_url, core_client.token, verify_ssl=False
    )
    pooled = websocket_manager.pooled_client(
        core_client.base_url, core_client.token, False
    )
    assert pooled is previous

    generator = session.fixture()
    try:
        async with running_home_assistant(hass):
            info = await hass.async_add_executor_job(next, generator)
        assert info["base_url"] == core_client.base_url
        assert os.environ["HOMEASSISTANT_URL"] == info["base_url"]
        assert config.get_global_settings().homeassistant_url == info["base_url"]
        # The next lookup must build a client of its own rather than reuse the
        # one pooled before the session.
        assert (
            websocket_manager.pooled_client(
                core_client.base_url, core_client.token, False
            )
            is None
        )
    finally:
        generator.close()
        await hass.async_add_executor_job(session.request.finalize)
        await previous.disconnect()
