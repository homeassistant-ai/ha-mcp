"""The server-side Marketplace auto-refresh nudge, on a real ``hass``.

On a startup where the version picture changed, the server asks Core's
Marketplace to re-fetch the ha_mcp_tools
component's repository info, so an external install surfaces the paired
component update instead of waiting out the store's cache.

The server's settings point at the real ``hass``, and every store command and
its reply is Core's (``_hacs_marketplace``: GitHub is unreachable, so a
refresh fails as Core fails it offline). The update check asks the test's own
PyPI on loopback (``_update_check_sources``) for the newest ha-mcp release.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.core import HomeAssistant
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp import __version__, hacs_auto_refresh
from ha_mcp._vendor import websockets
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.config import OAUTH_MODE_TOKEN, get_global_settings, reset_global_settings
from ha_mcp.hacs_auto_refresh import (
    MARKER_FILENAME_PREFIX,
    SETTING,
    SETTING_ENV,
    hacs_refresh_lifespan,
    maybe_refresh_hacs_after_update,
)
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.tools.hacs_backend import GITHUB_COMMAND_TIMEOUT
from ha_mcp.update_check import get_update_info
from ha_mcp.utils.data_paths import get_data_dir

from ._client_spies import sent_commands
from ._command_faults import received
from ._core_client import refused_url
from ._hacs_marketplace import command_types, set_up_marketplace, stored
from ._marketplace_records import REPOSITORY_IDS, own_records, unload_marketplace
from ._settings_ui_app import settings_http
from ._tools_addons_core import armed, outwait, timers
from ._update_check_sources import PyPI
from ._update_check_sources import pypi as pypi

MIRROR, MAIN = hacs_auto_refresh.CANDIDATE_REPO_FULL_NAMES
MIRROR_ID, MAIN_ID = REPOSITORY_IDS[MIRROR], REPOSITORY_IDS[MAIN]
OTHER_ID = "777000111"
LIST = "marketplace/repositories/list"
REFRESH = "marketplace/repository/refresh"
CURRENT = __version__
# A release newer than any build under test; PyPI would name it.
NEWER = "999.0.0"
# The last, longest retry delay: no command wait (the refresh's is the
# longest) reaches it, so a timer this far out is the nudge's own.
RETRY = hacs_auto_refresh.RETRY_DELAYS[-1]

Store = Callable[..., Awaitable[MarketplaceManager]]


def newest_release(pypi: PyPI, latest: str | None) -> None:
    """PyPI names ``latest`` as the newest ha-mcp of either channel (``None``:
    it knows no release), and the next start asks it again."""
    pypi.releases.clear()
    if latest is not None:
        pypi.publish("ha-mcp", latest)
        pypi.publish("ha-mcp-dev", latest)
    get_update_info.cache_clear()


def older_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server runs a build older than the one under test."""
    monkeypatch.setattr(hacs_auto_refresh, "get_version", lambda: "0.0.1")


@pytest.fixture
def server(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient, pypi: PyPI
) -> None:
    """The server's settings point at the real ``hass``, its update check is
    on and asks PyPI, the nudge is switched on and a newer release is out."""
    monkeypatch.setenv("HOMEASSISTANT_URL", core_client.base_url)
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", core_client.token)
    monkeypatch.delenv("HA_MCP_DISABLE_UPDATE_CHECK")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv(SETTING_ENV, "true")
    newest_release(pypi, NEWER)
    reset_global_settings()


def setting_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither a variable nor the settings page chose the nudge's setting."""
    monkeypatch.delenv(SETTING_ENV)
    reset_global_settings()


@pytest.fixture
async def store(
    hass: HomeAssistant,
    server: None,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> AsyncIterator[Store]:
    """Set Core's Marketplace up with the component's records as given."""

    async def set_up(
        *, main: bool | None = None, mirror: bool | None = None, other: bool = False
    ) -> MarketplaceManager:
        records = own_records(main=main, mirror=mirror)
        if other:
            records[OTHER_ID] = stored(
                "someone/other", "integration", installed="v1.0.0"
            )
        entry = await set_up_marketplace(hass, aioclient_mock, hass_storage, records)
        manager: MarketplaceManager = entry.runtime_data
        return manager

    yield set_up
    await unload_marketplace(hass)


@contextmanager
def github_answers(manager: MarketplaceManager, *repository_ids: str) -> Iterator[None]:
    """GitHub answers the refresh of each of ``repository_ids``; it stays
    unreachable for every other repository."""
    with ExitStack() as stack:
        for repository_id in repository_ids:
            repository = manager.repositories.get_by_id(repository_id)
            assert repository is not None
            stack.enter_context(
                patch.object(repository, "update_repository", autospec=True)
            )
        yield


def refreshed(seen: dict[str, list[Any]]) -> list[str]:
    return [msg["repository"] for msg in seen[REFRESH]]


def markers() -> list[Path]:
    """The completed passes' markers in the server's data directory."""
    return sorted(get_data_dir().glob(f"{MARKER_FILENAME_PREFIX}_*.json"))


@contextmanager
def logged(fragment: str) -> Iterator[asyncio.Event]:
    """Set once the nudge logs a line containing ``fragment``, at any level."""
    seen = asyncio.Event()

    class Watch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if fragment in record.getMessage():
                seen.set()

    log = logging.getLogger(hacs_auto_refresh.__name__)
    handler, level = Watch(), log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        yield seen
    finally:
        log.removeHandler(handler)
        log.setLevel(level)


class TestRefresh:
    async def test_first_startup_refreshes_both_installed_component_entries(
        self, hass: HomeAssistant, store: Store, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = await store(main=True, mirror=True, other=True)

        with (
            caplog.at_level(logging.INFO),
            github_answers(manager, MIRROR_ID, MAIN_ID),
            received(hass, LIST, REFRESH) as seen,
        ):
            await maybe_refresh_hacs_after_update()

        # Both entries the component can be tracked under, and nothing else.
        assert sorted(refreshed(seen)) == sorted([MIRROR_ID, MAIN_ID])
        assert len(seen[LIST]) == 1
        # The due-pass line is what the HAOS add-on lane greps for.
        assert "Marketplace auto-refresh: startup pass due" in caplog.text
        # The one info line is the operator-visible signal for this feature.
        assert MIRROR in caplog.text
        assert MAIN in caplog.text
        assert markers()

    async def test_embedded_server_refreshes_its_component_too(
        self, hass: HomeAssistant, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its component update is the one the Marketplace never checks for
        without a connected GitHub account."""
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        manager = await store(main=True)

        with github_answers(manager, MAIN_ID), received(hass, REFRESH) as seen:
            await maybe_refresh_hacs_after_update()

        assert refreshed(seen) == [MAIN_ID]

    async def test_a_downloaded_entry_only_is_refreshed(
        self, hass: HomeAssistant, store: Store
    ) -> None:
        """Migration limbo: the mirror is added but not downloaded, so only the
        main entry has an update entity to light up."""
        manager = await store(main=True, mirror=False)

        with (
            github_answers(manager, MIRROR_ID, MAIN_ID),
            received(hass, REFRESH) as seen,
        ):
            await maybe_refresh_hacs_after_update()

        assert refreshed(seen) == [MAIN_ID]

    async def test_nothing_to_refresh_still_completes_the_pass(
        self, hass: HomeAssistant, store: Store
    ) -> None:
        """The next startup on the same version does not ask the store again."""
        await store(other=True)

        with received(hass, LIST, REFRESH) as seen:
            await maybe_refresh_hacs_after_update()
            await maybe_refresh_hacs_after_update()

        assert len(seen[LIST]) == 1
        assert seen[REFRESH] == []

    async def test_one_failed_refresh_does_not_cost_the_other_its_refresh(
        self, hass: HomeAssistant, store: Store
    ) -> None:
        manager = await store(main=True, mirror=True)

        with github_answers(manager, MAIN_ID), received(hass, LIST, REFRESH) as seen:
            await maybe_refresh_hacs_after_update()
            await maybe_refresh_hacs_after_update()

        # Both were asked, and the pass counted: the second startup sent nothing.
        assert sorted(refreshed(seen)) == sorted([MIRROR_ID, MAIN_ID])
        assert len(seen[LIST]) == 1

    async def test_every_refresh_failing_is_retried_at_the_next_startup(
        self, hass: HomeAssistant, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An undetermined pass must not be recorded as done."""
        monkeypatch.setattr(hacs_auto_refresh, "RETRY_DELAYS", ())
        await store(main=True, mirror=True)

        with received(hass, REFRESH) as seen:
            await maybe_refresh_hacs_after_update()
            assert len(seen[REFRESH]) == 2
            assert markers() == []
            await maybe_refresh_hacs_after_update()

        assert len(seen[REFRESH]) == 4


class TestWhenDue:
    async def test_a_server_update_makes_the_pass_due(
        self, hass: HomeAssistant, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The just-updated case: the paired component release is what the
        store needs to surface."""
        manager = await store(main=True)
        with github_answers(manager, MAIN_ID), received(hass, REFRESH) as seen:
            with monkeypatch.context() as before_the_update:
                older_server(before_the_update)
                await maybe_refresh_hacs_after_update()
            await maybe_refresh_hacs_after_update()

        assert refreshed(seen) == [MAIN_ID, MAIN_ID]

    async def test_a_release_since_the_last_pass_makes_it_due(
        self, hass: HomeAssistant, store: Store, pypi: PyPI
    ) -> None:
        manager = await store(main=True)
        with github_answers(manager, MAIN_ID), received(hass, REFRESH) as seen:
            newest_release(pypi, CURRENT)
            await maybe_refresh_hacs_after_update()
            newest_release(pypi, NEWER)
            await maybe_refresh_hacs_after_update()

        assert refreshed(seen) == [MAIN_ID, MAIN_ID]

    @pytest.mark.parametrize(
        "latest",
        [
            pytest.param(NEWER, id="release-already-surfaced"),
            pytest.param(CURRENT, id="no-newer-release"),
            pytest.param(None, id="update-check-unanswered"),
        ],
    )
    async def test_an_unchanged_picture_sends_nothing_and_logs_quietly(
        self,
        hass: HomeAssistant,
        store: Store,
        pypi: PyPI,
        caplog: pytest.LogCaptureFixture,
        latest: str | None,
    ) -> None:
        """The stdio hot path: a per-conversation spawn on an unchanged version
        costs one file read, no store traffic and no log noise."""
        manager = await store(main=True)
        with github_answers(manager, MAIN_ID):
            await maybe_refresh_hacs_after_update()
        newest_release(pypi, latest)
        caplog.clear()

        with caplog.at_level(logging.DEBUG), received(hass, LIST, REFRESH) as seen:
            await maybe_refresh_hacs_after_update()

        assert seen == {LIST: [], REFRESH: []}
        assert "startup pass due" not in caplog.text
        # The not-due DEBUG line is what the HAOS add-on lane greps for once
        # an earlier completed pass makes boots not due.
        not_due = [
            record
            for record in caplog.records
            if "Marketplace auto-refresh: pass not due" in record.getMessage()
        ]
        assert [record.levelno for record in not_due] == [logging.DEBUG]

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("{not json", id="unparseable"),
            pytest.param('["nope"]', id="not-an-object"),
        ],
    )
    async def test_an_unreadable_marker_makes_the_pass_due(
        self, hass: HomeAssistant, store: Store, content: str
    ) -> None:
        manager = await store(main=True)
        with github_answers(manager, MAIN_ID), received(hass, REFRESH) as seen:
            await maybe_refresh_hacs_after_update()
            [marker] = markers()
            marker.write_text(content, encoding="utf-8")
            await maybe_refresh_hacs_after_update()

        assert refreshed(seen) == [MAIN_ID, MAIN_ID]

    async def test_each_home_assistant_keeps_its_own_marker(
        self,
        hass: HomeAssistant,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Server configs sharing a data dir but targeting different instances:
        neither suppresses the other's pass nor invalidates its marker."""
        monkeypatch.setattr(hacs_auto_refresh, "RETRY_DELAYS", ())
        manager = await store(main=True)
        with github_answers(manager, MAIN_ID), received(hass, REFRESH) as seen:
            await maybe_refresh_hacs_after_update()
            monkeypatch.setenv("HOMEASSISTANT_URL", refused_url())
            reset_global_settings()
            caplog.clear()
            with caplog.at_level(logging.INFO):
                await maybe_refresh_hacs_after_update()
            assert "Marketplace auto-refresh: startup pass due" in caplog.text
            # Back to the first one, written with a trailing slash: the same target.
            monkeypatch.setenv("HOMEASSISTANT_URL", core_client.base_url + "/")
            reset_global_settings()
            await maybe_refresh_hacs_after_update()

        assert refreshed(seen) == [MAIN_ID]


class TestSwitch:
    async def test_a_new_install_has_it_off(
        self, hass: HomeAssistant, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await store(main=True)
        setting_unset(monkeypatch)

        with received(hass, LIST) as seen:
            await maybe_refresh_hacs_after_update()

        assert seen[LIST] == []
        reset_global_settings()
        assert getattr(get_global_settings(), SETTING) is False

    async def test_an_install_that_already_ran_it_keeps_it_on(
        self, hass: HomeAssistant, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its marker shows the nudge ran before it became opt-in; with no
        explicit choice the setting is saved on, so nothing changes for it."""
        manager = await store(main=True)
        with github_answers(manager, MAIN_ID), received(hass, REFRESH) as seen:
            with monkeypatch.context() as before_the_update:
                older_server(before_the_update)
                await maybe_refresh_hacs_after_update()
            setting_unset(monkeypatch)
            await maybe_refresh_hacs_after_update()

        assert refreshed(seen) == [MAIN_ID, MAIN_ID]
        # Saved for the next start, not just switched on for this run.
        reset_global_settings()
        assert getattr(get_global_settings(), SETTING) is True

    @pytest.mark.parametrize("where", ["variable", "settings page"])
    async def test_an_explicit_off_is_kept_even_with_a_marker(
        self,
        hass: HomeAssistant,
        store: Store,
        monkeypatch: pytest.MonkeyPatch,
        where: str,
    ) -> None:
        manager = await store(main=True)
        with github_answers(manager, MAIN_ID), received(hass, LIST) as seen:
            with monkeypatch.context() as before_the_update:
                older_server(before_the_update)
                await maybe_refresh_hacs_after_update()
            setting_unset(monkeypatch)
            if where == "variable":
                monkeypatch.setenv(SETTING_ENV, "false")
            else:
                async with settings_http() as http:
                    saved = await http.post(
                        "/api/settings/advanced", json={SETTING: False}
                    )
                assert saved.status_code == 200
            reset_global_settings()
            await maybe_refresh_hacs_after_update()

        assert len(seen[LIST]) == 1
        reset_global_settings()
        assert getattr(get_global_settings(), SETTING) is False

    async def test_a_disabled_update_check_switches_it_off(
        self, hass: HomeAssistant, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator who opted out of the update phone-home gets no
        update-driven store traffic either."""
        await store(main=True)
        monkeypatch.setenv("HA_MCP_DISABLE_UPDATE_CHECK", "1")

        with received(hass, LIST) as seen:
            await maybe_refresh_hacs_after_update()

        assert seen[LIST] == []
        assert markers() == []

    async def test_oauth_mode_sends_nothing(
        self, hass: HomeAssistant, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-user OAuth mode holds no server-level credential; the nudge
        would only burn its retry schedule on auth failures."""
        await store(main=True)
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", OAUTH_MODE_TOKEN)
        reset_global_settings()

        with received(hass, LIST) as seen:
            await maybe_refresh_hacs_after_update()

        assert seen[LIST] == []
        assert markers() == []


class TestRetries:
    async def test_no_store_is_recorded_only_after_the_retries(
        self, hass: HomeAssistant, server: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown command is also what a still-booting Home Assistant
        answers before the store registers its commands, so absence must
        survive the retry schedule before it is recorded."""
        monkeypatch.setattr(hacs_auto_refresh, "RETRY_DELAYS", (RETRY,))
        socket = await get_websocket_client()
        besides = timers(hass)

        with sent_commands(socket) as spy:
            task = asyncio.create_task(maybe_refresh_hacs_after_update())
            await armed(hass, GITHUB_COMMAND_TIMEOUT, RETRY, besides)
            assert markers() == []
            await outwait(hass, GITHUB_COMMAND_TIMEOUT, RETRY, besides)
            await asyncio.gather(task)
            # One list per attempt; nothing else is asked.
            assert command_types(spy) == [LIST] * 2
            await maybe_refresh_hacs_after_update()

        assert len(markers()) == 1
        assert len(spy.call_args_list) == 2

    async def test_an_unreachable_home_assistant_is_retried_at_the_next_startup(
        self,
        server: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nothing was determined, so no pass is recorded; the due line is
        still logged, as the HAOS add-on lane (no store there) needs."""
        monkeypatch.setattr(hacs_auto_refresh, "RETRY_DELAYS", ())
        monkeypatch.setenv("HOMEASSISTANT_URL", refused_url())
        reset_global_settings()

        with caplog.at_level(logging.DEBUG):
            await maybe_refresh_hacs_after_update()

        assert "Marketplace auto-refresh: startup pass due" in caplog.text
        assert "Marketplace repository refresh attempt failed" in caplog.text
        assert markers() == []

    async def test_a_socket_closed_mid_send_uses_a_retry(
        self, hass: HomeAssistant, server: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A closure mid-send reaches the nudge as the raw websocket error
        (ambiguous-write contract); it must take a retry, not end the nudge."""
        monkeypatch.setattr(hacs_auto_refresh, "RETRY_DELAYS", (RETRY,))
        socket = await get_websocket_client()
        besides = timers(hass)

        with patch.object(
            socket,
            "send_command",
            AsyncMock(side_effect=websockets.exceptions.WebSocketException("closed")),
        ):
            task = asyncio.create_task(maybe_refresh_hacs_after_update())
            await outwait(hass, GITHUB_COMMAND_TIMEOUT, RETRY, besides)
            await asyncio.gather(task)

        assert markers() == []


class TestLifespan:
    async def test_leaving_the_lifespan_cancels_a_nudge_waiting_to_retry(
        self, server: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A nudge parked in a retry sleep cannot stall shutdown."""
        monkeypatch.setenv("HOMEASSISTANT_URL", refused_url())
        reset_global_settings()

        with logged("Marketplace repository refresh attempt failed") as failed:
            async with hacs_refresh_lifespan(None):
                await failed.wait()
                [nudge] = [
                    task
                    for task in asyncio.all_tasks()
                    if getattr(task.get_coro(), "__qualname__", None)
                    == maybe_refresh_hacs_after_update.__qualname__
                ]

        await asyncio.wait([nudge])
        assert nudge.cancelled()

    async def test_the_server_runs_the_nudge_when_it_starts(
        self,
        server: None,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
    ) -> None:
        """Every launcher, the app's ``start.py`` included, starts the server's
        own lifespan: dropping the nudge from it is the gap found live."""
        setting_unset(monkeypatch)
        mcp_server = HomeAssistantSmartMCPServer(client=core_client)
        app = mcp_server.mcp.http_app()

        with logged("Marketplace auto-refresh: off") as ran:
            async with app.router.lifespan_context(app):
                await ran.wait()
