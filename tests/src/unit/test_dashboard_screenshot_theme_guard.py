"""The dashboard-screenshot theme guard (issue #1909), on a real Core.

Puppet dispatches a ``settheme`` event on cold renders, and Home Assistant
saves it on the engine token user's profile, flipping that user's real
sessions. The guard reads the saved theme before and after a capture and
reports a change so the agent can undo it with ``ha_manage_theme``; it never
writes itself, which keeps the screenshot tools honestly ``readOnlyHint:
True``. Core's ``frontend`` keeps every saved theme here, and the engine signs
in as a Core user of its own.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockUser

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.dashboard_screenshot import capture, theme_guard
from ha_mcp.dashboard_screenshot.theme_guard import (
    EngineCredential,
    ThemeChangedError,
    ThemeGuard,
    addon_credential_from_options,
    read_engine_theme,
    refusal_reason,
    write_engine_theme,
)
from ha_mcp.tools.tools_themes import ThemesTools

from ._command_faults import received, unanswered
from ._core_client import refused_url
from ._dashboard_screenshot_common import PNG, clear_screenshot_env, set_settings
from ._dashboard_screenshot_theme_guard_core import (
    CLOBBERED,
    DARK,
    Account,
    closes_running,
    core_sessions,
    render_report,
    save_theme,
    saved_theme,
    serve_engine,
    serve_puppet,
    set_up_frontend,
)
from ._tools_addons_core import Supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app
from ._tool_errors import error_body
from ._websocket_client_core import held_auth, short_cleanup
from ._ws_transport import until

GET = "frontend/get_user_data"
SET = "frontend/set_user_data"
_PUPPET_URL = "http://homeassistant:8123"
_PUPPET = EngineCredential(url=_PUPPET_URL, token="puppet-token")


@pytest.fixture(autouse=True)
def _standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Supervisor and no engine URL, unless a test sets one up."""
    clear_screenshot_env(monkeypatch)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    set_settings(monkeypatch, dashboard_screenshot_engine_url="", read_only_mode=False)
    # Core saves at once; only a live engine's late save needs the wait.
    monkeypatch.setattr(theme_guard, "RESTORE_SETTLE_SECONDS", 0)


@pytest.fixture
def engine_signed_out(
    core_client: HomeAssistantClient,
    hass_read_only_user: MockUser,
    hass_read_only_access_token: str,
) -> Account:
    """The engine's own Core user and token, on a Core without a frontend."""
    return Account(
        hass_read_only_user,
        EngineCredential(url=core_client.base_url, token=hass_read_only_access_token),
    )


@pytest.fixture
async def engine(
    hass: HomeAssistant, engine_signed_out: Account, tmp_path: Path
) -> Account:
    """The engine's own Core user, on a Core that keeps saved themes."""
    await set_up_frontend(hass, tmp_path)
    return engine_signed_out


@pytest.fixture
def own(
    core_client: HomeAssistantClient, hass_admin_user: MockUser, engine: Account
) -> Account:
    """ha-mcp's own Core user: the account ``core_client`` signs in as."""
    return Account(
        hass_admin_user,
        EngineCredential(url=core_client.base_url, token=core_client.token),
    )


@pytest.fixture
def seen(hass: HomeAssistant, engine: Account) -> Iterator[dict[str, list[Any]]]:
    """The theme reads and writes Core receives."""
    with received(hass, GET, SET) as messages:
        yield messages


def _quoted(report: str, field: str) -> Any:
    """The JSON value the report quotes for ``field``."""
    found = re.search(rf"{field}=(\{{.*?\}})", report)
    assert found, f"{field} not quoted in: {report}"
    return json.loads(found.group(1))


class TestCredentialResolution:
    def test_puppet_options_give_the_engine_token_and_url(self) -> None:
        options = {
            "access_token": "puppet-token",
            "keep_browser_open": False,
            "home_assistant_url": _PUPPET_URL,
        }
        assert addon_credential_from_options(options) == _PUPPET

    def test_puppet_without_a_url_reaches_core_at_the_supervisor_alias(
        self,
    ) -> None:
        options = {"access_token": "puppet-token", "home_assistant_url": ""}
        assert addon_credential_from_options(options) == _PUPPET

    def test_puppet_without_a_token_gives_no_credential(self) -> None:
        options = {"access_token": "", "home_assistant_url": _PUPPET_URL}
        assert addon_credential_from_options(options) is None
        assert addon_credential_from_options(None) is None

    def test_puppet_credential_wins_and_is_provably_the_engine(
        self, core_client: HomeAssistantClient
    ) -> None:
        guard = ThemeGuard.for_capture(_PUPPET, core_client)
        assert guard.credential == _PUPPET
        assert guard.credential_is_engine is True

    def test_ha_mcp_own_credential_is_the_unproven_fallback(
        self, core_client: HomeAssistantClient
    ) -> None:
        guard = ThemeGuard.for_capture(None, core_client)
        # verify_ssl is carried: a self-signed Core would refuse every session.
        assert guard.credential == EngineCredential(
            url=core_client.base_url, token=core_client.token, verify_ssl=False
        )
        assert guard.credential_is_engine is False

    def test_addon_mode_has_no_fallback(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Supervisor proxy signs in as the Supervisor's own user, whose
        profile the engine never touches."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        assert ThemeGuard.for_capture(None, core_client).credential is None

    def test_embedded_mode_keeps_the_fallback(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core's container carries SUPERVISOR_TOKEN, but the embedded server
        signs in with a real user's token."""
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        assert ThemeGuard.for_capture(None, core_client).credential is not None

    @pytest.mark.parametrize(
        ("base_url", "token"),
        [("homeassistant.local:8123", "own-token"), ("http://ha.local:8123", "")],
        ids=["no-http-url", "no-token"],
    )
    async def test_a_client_that_cannot_sign_in_gives_no_fallback(
        self, base_url: str, token: str
    ) -> None:
        client = HomeAssistantClient(base_url=base_url, token=token, verify_ssl=True)
        try:
            assert ThemeGuard.for_capture(None, client).credential is None
        finally:
            await client.close()

    def test_no_credential_at_all_leaves_the_guard_off(self) -> None:
        assert ThemeGuard.for_capture(None, None).credential is None


class TestDetection:
    async def test_a_flipped_theme_is_reported_and_left_as_is(
        self, hass: HomeAssistant, engine: Account, seen: dict[str, list[Any]]
    ) -> None:
        """#1909, read-only form: the change is reported, never undone."""
        guard = ThemeGuard.for_capture(engine.credential, None)

        report = await render_report(hass, guard, engine)

        assert seen[SET] == []
        assert await saved_theme(hass, engine) == CLOBBERED
        assert guard.changed_from == DARK
        assert "ha_manage_theme" in report
        assert "set_engine_theme" in report
        assert "own Home Assistant user" in report

    async def test_the_engine_account_is_watched_not_ha_mcps_own(
        self,
        hass: HomeAssistant,
        engine: Account,
        own: Account,
        core_client: HomeAssistantClient,
    ) -> None:
        await save_theme(hass, own, {"theme": "nord"})
        guard = ThemeGuard.for_capture(engine.credential, core_client)

        await render_report(hass, guard, engine)

        assert guard.changed_from == DARK

    async def test_an_unchanged_theme_is_not_reported(
        self, hass: HomeAssistant, engine: Account
    ) -> None:
        await save_theme(hass, engine, DARK)
        guard = ThemeGuard.for_capture(engine.credential, None)

        await guard.take_snapshot()
        await guard.detect_change()

        assert guard.warnings == []
        assert guard.changed_from is None

    async def test_a_never_saved_theme_is_restored_as_empty_settings(
        self, hass: HomeAssistant, engine: Account
    ) -> None:
        """A null restore is ignored by live sessions; {} re-applies the default."""
        guard = ThemeGuard.for_capture(engine.credential, None)
        await guard.take_snapshot()
        await save_theme(hass, engine, CLOBBERED)

        await guard.detect_change()

        assert _quoted(guard.warnings[0], "value") == {}
        assert _quoted(guard.warnings[0], "expected_current") == CLOBBERED

    async def test_each_read_closes_its_session(
        self, hass: HomeAssistant, engine: Account
    ) -> None:
        guard = ThemeGuard.for_capture(engine.credential, None)
        with core_sessions(hass) as sessions:
            await render_report(hass, guard, engine)
            await until(lambda: sessions.closed == 2)
        assert sessions.opened == 2

    async def test_a_guard_without_credential_contacts_nothing(
        self, hass: HomeAssistant, seen: dict[str, list[Any]]
    ) -> None:
        guard = ThemeGuard.for_capture(None, None)
        with core_sessions(hass) as sessions:
            await guard.take_snapshot()
            await guard.detect_change()
        assert sessions.opened == 0
        assert seen[GET] == []
        assert guard.warnings == []

    async def test_an_unreadable_snapshot_is_reported_and_stops_detection(
        self,
        hass: HomeAssistant,
        engine_signed_out: Account,
        tmp_path: Path,
    ) -> None:
        """Core without its frontend refuses the read; with no trustworthy
        snapshot there is nothing to compare against."""
        guard = ThemeGuard.for_capture(engine_signed_out.credential, None)
        await guard.take_snapshot()
        assert len(guard.warnings) == 1
        assert "before rendering" in guard.warnings[0]

        await set_up_frontend(hass, tmp_path)
        await save_theme(hass, engine_signed_out, CLOBBERED)
        with received(hass, GET) as reads:
            await guard.detect_change()

        assert reads[GET] == []
        assert guard.changed_from is None
        assert len(guard.warnings) == 1

    async def test_an_unreachable_core_is_reported_without_raising(
        self, engine: Account
    ) -> None:
        credential = EngineCredential(url=refused_url(), token=engine.credential.token)
        guard = ThemeGuard.for_capture(credential, None)

        await guard.take_snapshot()
        await guard.detect_change()

        assert len(guard.warnings) == 1
        assert "before rendering" in guard.warnings[0]

    async def test_a_theme_saved_after_the_render_returned_is_still_seen(
        self,
        hass: HomeAssistant,
        engine: Account,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The frontend saves Puppet's settheme after the image is back; a read
        racing that save would miss the flip."""
        # Shortened from 1 s; the save lands halfway through the wait, long
        # after a loopback read with no wait would have finished.
        settle = 0.2
        monkeypatch.setattr(theme_guard, "RESTORE_SETTLE_SECONDS", settle)
        await save_theme(hass, engine, DARK)
        guard = ThemeGuard.for_capture(engine.credential, None)
        await guard.take_snapshot()

        hass.loop.call_later(
            settle / 2,
            lambda: hass.async_create_task(save_theme(hass, engine, CLOBBERED)),
        )
        await guard.detect_change()

        assert guard.changed_from == DARK

    async def test_an_unanswered_re_read_is_reported_without_raising(
        self, hass: HomeAssistant, engine: Account, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await save_theme(hass, engine, DARK)
        guard = ThemeGuard.for_capture(engine.credential, None)
        await guard.take_snapshot()
        monkeypatch.setattr(theme_guard, "COMMAND_TIMEOUT_SECONDS", 0.05)

        async with unanswered(hass, GET, monkeypatch):
            await guard.detect_change()

        assert len(guard.warnings) == 1
        assert "Could not check whether" in guard.warnings[0]


class TestEngineThemeWrites:
    """The write path lives behind ha_manage_theme, not the capture path."""

    async def test_the_saved_theme_is_read_back(
        self, hass: HomeAssistant, engine: Account
    ) -> None:
        await save_theme(hass, engine, DARK)
        assert await read_engine_theme(engine.credential) == DARK

    async def test_force_overwrites_whatever_is_saved(
        self, hass: HomeAssistant, engine: Account, seen: dict[str, list[Any]]
    ) -> None:
        await save_theme(hass, engine, DARK)

        await write_engine_theme(
            engine.credential, CLOBBERED, expected_current=None, force=True
        )

        assert await saved_theme(hass, engine) == CLOBBERED
        assert len(seen[SET]) == 1

    async def test_a_theme_changed_since_the_warning_is_not_overwritten(
        self, hass: HomeAssistant, engine: Account, seen: dict[str, list[Any]]
    ) -> None:
        await save_theme(hass, engine, {"theme": "nord"})

        with pytest.raises(ThemeChangedError) as exc_info:
            await write_engine_theme(
                engine.credential, DARK, expected_current=CLOBBERED
            )

        assert exc_info.value.actual == {"theme": "nord"}
        assert seen[SET] == []
        assert await saved_theme(hass, engine) == {"theme": "nord"}

    async def test_the_expected_theme_is_overwritten(
        self, hass: HomeAssistant, engine: Account
    ) -> None:
        await save_theme(hass, engine, CLOBBERED)

        await write_engine_theme(engine.credential, DARK, expected_current=CLOBBERED)

        assert await saved_theme(hass, engine) == DARK

    async def test_null_expected_current_refuses_a_theme_saved_since(
        self, hass: HomeAssistant, engine: Account, seen: dict[str, list[Any]]
    ) -> None:
        """The warning saw no theme; by the restore the user has picked one."""
        await save_theme(hass, engine, DARK)

        with pytest.raises(ThemeChangedError):
            await write_engine_theme(
                engine.credential, CLOBBERED, expected_current=None
            )

        assert seen[SET] == []

    async def test_null_expected_current_writes_when_nothing_is_saved(
        self, hass: HomeAssistant, engine: Account
    ) -> None:
        await write_engine_theme(engine.credential, DARK, expected_current=None)

        assert await saved_theme(hass, engine) == DARK


class TestCaptureBracket:
    """A capture reads the engine user's theme and reports changes, never writes.

    The no-write property is what keeps the screenshot and dashboard-get tools
    honestly ``readOnlyHint: True`` (#1991, PR #2014).
    """

    async def test_a_capture_reports_the_flip_and_writes_nothing(
        self, supervisor: Supervisor, aiohttp_client: Any,
        hass: HomeAssistant,
        engine: Account,
        seen: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        serve_puppet(monkeypatch, supervisor, engine.credential)
        await serve_engine(aiohttp_client, monkeypatch, hass, flips=engine)
        await save_theme(hass, engine, DARK)
        warnings: list[str] = []

        captures = await capture.capture_dashboard_images(
            "lovelace/0", capture_warnings=warnings
        )

        assert captures[0].data == PNG
        assert len(seen[GET]) == 2
        assert seen[SET] == []
        assert await saved_theme(hass, engine) == CLOBBERED
        assert len(warnings) == 1
        assert "ha_manage_theme" in warnings[0]

    async def test_an_engine_that_keeps_the_theme_adds_no_warning(
        self, supervisor: Supervisor, aiohttp_client: Any, hass: HomeAssistant, engine: Account, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve_puppet(monkeypatch, supervisor, engine.credential)
        await serve_engine(aiohttp_client, monkeypatch, hass, flips=None)
        await save_theme(hass, engine, DARK)
        warnings: list[str] = []

        await capture.capture_dashboard_images("lovelace/0", capture_warnings=warnings)

        assert warnings == []
        assert await saved_theme(hass, engine) == DARK

    async def test_a_sidecar_engine_is_watched_with_ha_mcps_own_credential(
        self, aiohttp_client: Any,
        hass: HomeAssistant,
        own: Account,
        core_client: HomeAssistantClient,
        seen: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Docker / standalone: the engine runs with ha-mcp's own user."""
        set_settings(monkeypatch, dashboard_screenshot_engine_url="http://engine:10000")
        await serve_engine(aiohttp_client, monkeypatch, hass, flips=own)
        await save_theme(hass, own, DARK)
        warnings: list[str] = []

        captures = await capture.capture_dashboard_images(
            "lovelace/0", client=core_client, capture_warnings=warnings
        )

        assert captures[0].data == PNG
        assert len(warnings) == 1
        assert seen[SET] == []

    async def test_a_failed_render_still_reports_the_flip(
        self, supervisor: Supervisor, aiohttp_client: Any,
        hass: HomeAssistant,
        engine: Account,
        seen: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        serve_puppet(monkeypatch, supervisor, engine.credential)
        await serve_engine(aiohttp_client, monkeypatch, hass, flips=engine, status=500)
        await save_theme(hass, engine, DARK)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0")

        assert seen[SET] == []
        warnings = error_body(exc_info).get("warnings", [])
        assert any("ha_manage_theme" in warning for warning in warnings)


class TestRestoreThroughTheTool:
    """ha_manage_theme's engine actions, with the credential Puppet's options give."""

    async def test_the_restore_the_warning_names_puts_the_theme_back(
        self, supervisor: Supervisor,
        hass: HomeAssistant,
        engine: Account,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both quoted values are JSON the tool accepts, and the call it names
        restores the theme the render replaced."""
        serve_puppet(monkeypatch, supervisor, engine.credential)
        guard = ThemeGuard.for_capture(engine.credential, None)
        report = await render_report(hass, guard, engine)
        assert "Profile > General" not in report

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set_engine_theme",
            value=_quoted(report, "value"),
            expected_current=_quoted(report, "expected_current"),
        )

        assert result["success"] is True
        assert await saved_theme(hass, engine) == DARK

    async def test_force_reaches_the_write(
        self, supervisor: Supervisor,
        hass: HomeAssistant,
        engine: Account,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """expected_current mismatches: only force passed down lands the write."""
        serve_puppet(monkeypatch, supervisor, engine.credential)
        await save_theme(hass, engine, DARK)

        result = await ThemesTools(core_client).ha_manage_theme(
            action="set_engine_theme",
            value=CLOBBERED,
            expected_current=None,
            force=True,
        )

        assert result["success"] is True
        assert await saved_theme(hass, engine) == CLOBBERED

    async def test_without_force_a_mismatch_is_refused(
        self, supervisor: Supervisor,
        hass: HomeAssistant,
        engine: Account,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        serve_puppet(monkeypatch, supervisor, engine.credential)
        await save_theme(hass, engine, DARK)

        with pytest.raises(ToolError):
            await ThemesTools(core_client).ha_manage_theme(
                action="set_engine_theme",
                value=CLOBBERED,
                expected_current=None,
                force=False,
            )

        assert await saved_theme(hass, engine) == DARK

    async def test_an_explicit_engine_url_is_refused_as_unidentifiable(
        self,
        core_client: HomeAssistantClient,
        seen: dict[str, list[Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The only credential left is ha-mcp's own, a different user under the
        dedicated-account setup this feature recommends."""
        set_settings(
            monkeypatch, dashboard_screenshot_engine_url="http://sidecar:10000"
        )

        with pytest.raises(ToolError) as exc_info:
            await ThemesTools(core_client).ha_manage_theme(action="get_engine_theme")

        assert "cannot be identified" in str(exc_info.value)
        assert seen[GET] == []

    async def test_puppets_credential_reads_the_engine_account(
        self, supervisor: Supervisor,
        hass: HomeAssistant,
        engine: Account,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        serve_puppet(monkeypatch, supervisor, engine.credential)
        await save_theme(hass, engine, DARK)

        result = await ThemesTools(core_client).ha_manage_theme(
            action="get_engine_theme"
        )

        assert result["data"]["theme"] == DARK


class TestCleartextRefusal:
    """The engine token is never sent in cleartext to a remote host."""

    @pytest.mark.parametrize(
        ("url", "refused"),
        [
            # The Supervisor-internal alias, mDNS and private literals stay on
            # the local network, the documented trusted zone.
            ("http://homeassistant:8123", False),
            ("http://ha.local:8123", False),
            ("http://10.0.0.5:8123", False),
            ("https://ha.example.com:8123", False),
            ("http://ha.example.com:8123", True),
            # Python's ipaddress calls RFC 5737 TEST-NET private, so a truly
            # public address stands in for "remote".
            ("http://8.8.8.8:8123", True),
            # A DNS name that merely looks private resolves wherever its owner
            # points it.
            ("http://10.attacker.example:8123", True),
            # The transport maps only https to wss: wss:// would go out as ws.
            ("wss://ha.example.com:8123", True),
        ],
    )
    def test_cleartext_policy(self, url: str, refused: bool) -> None:
        assert (refusal_reason(url) is not None) is refused

    async def test_a_remote_cleartext_url_is_never_contacted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        guard = ThemeGuard.for_capture(
            EngineCredential(url="http://ha.example.com:8123", token="t"), None
        )
        with caplog.at_level(logging.WARNING, logger=theme_guard.__name__):
            await guard.take_snapshot()

        assert guard.warnings
        assert "refusing to send" in caplog.text

    def test_local_wss_is_refused_as_an_unsupported_scheme(self) -> None:
        """http://homeassistant is allowed and is the same wire URL, so
        "cleartext to a remote host" would be false advice here."""
        reason = refusal_reason("wss://homeassistant:8123")
        assert reason is not None
        assert "remote host" not in reason
        assert "unsupported scheme" in reason

    def test_remote_http_keeps_the_cleartext_reason(self) -> None:
        reason = refusal_reason("http://ha.example.com:8123")
        assert reason is not None
        assert "remote host" in reason


class TestRemedy:
    """The report names the remedy that actually applies."""

    async def test_an_unproven_account_is_not_offered_the_tool(
        self, hass: HomeAssistant, own: Account, core_client: HomeAssistantClient
    ) -> None:
        """The engine-theme actions refuse exactly this credential."""
        guard = ThemeGuard.for_capture(None, core_client)

        report = await render_report(hass, guard, own)

        assert "cannot confirm which account" in report
        assert "ha_manage_theme" not in report
        assert "Profile > General" in report

    async def test_an_unproven_account_wins_over_read_only_mode(
        self,
        hass: HomeAssistant,
        own: Account,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Turning Read Only Mode off would not lift the identity refusal."""
        set_settings(monkeypatch, read_only_mode=True)
        guard = ThemeGuard.for_capture(None, core_client)

        report = await render_report(hass, guard, own)

        assert "cannot confirm which account" in report
        assert "turn Read Only Mode off" not in report

    async def test_read_only_mode_report_names_no_blocked_action(
        self, hass: HomeAssistant, engine: Account, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_settings(monkeypatch, read_only_mode=True)
        guard = ThemeGuard.for_capture(engine.credential, None)

        report = await render_report(hass, guard, engine)

        assert "Read Only Mode is on" in report
        assert "set_engine_theme" not in report
        assert "Profile > General" in report


class TestSessionCleanup:
    """A session Core never lets finish signing in leaves nothing behind."""

    @pytest.fixture(autouse=True)
    def _short_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        short_cleanup(monkeypatch)
        monkeypatch.setattr(theme_guard, "CLOSE_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(theme_guard, "SESSION_TIMEOUT_SECONDS", 0.05)

    async def test_a_sign_in_that_never_finishes_ends_at_the_deadline(
        self, hass: HomeAssistant, engine: Account, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """read_engine_theme sets no deadline of its own; the session's holds."""
        with core_sessions(hass) as sessions:
            async with held_auth(monkeypatch):
                with pytest.raises(TimeoutError):
                    await read_engine_theme(engine.credential)
            await until(lambda: sessions.closed == 1)

    async def test_a_close_core_never_answers_is_not_left_running(
        self, hass: HomeAssistant, engine: Account, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with core_sessions(hass) as sessions:
            async with held_auth(monkeypatch):
                with pytest.raises(TimeoutError):
                    await read_engine_theme(engine.credential)
                # Core has not answered the close yet, and nothing still waits.
                assert sessions.closed == 0
                assert closes_running() == []
            await until(lambda: sessions.closed == 1)
