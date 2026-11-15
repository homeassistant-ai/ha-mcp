"""The blueprint text acquisition ladder (``tools/blueprint_sources.py``, #2329).

Core's ``blueprint/list`` never returns a blueprint's file body, so four
different installations answer "give me this blueprint's YAML" four different
ways: an in-process server reads the file itself, the ha_mcp_tools component
reads it over WebSocket, the File & YAML Tools entry reads it as a privileged
service, or the blueprint's recorded ``source_url`` is re-downloaded. Both
``ha_manage_blueprints(action="get")`` and the pre-write auto-backup snapshot
walk this one ladder, so these tests pin its order, the jail on the direct read,
and the parse that turns whichever text won into a display body.

The direct tier runs against REAL files under ``tmp_path`` with a REAL jail — a
mocked path check would prove nothing about traversal. The other tiers run on
a real ``hass``: the component's ``blueprint_get`` reads the blueprint file in
Core's config directory, the File & YAML Tools entry is set up by Core, and
Core's ``blueprint/import`` downloads the source URL through ``aioclient_mock``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import blueprint_sources
from ha_mcp.tools.blueprint_sources import (
    parse_blueprint_body,
    resolve_blueprint_source,
)

from ._component_frames import drop_connection_on, held, older_component, pooled
from ._core_setup import set_up_tools_entry

_PATH = "user/motion.yaml"
_BLUEPRINT_GET = "ha_mcp_tools/blueprint_get"
# Core names a blueprint imported from a generic URL "<host>/<file stem>".
_SOURCE_URL = "https://example.com/motion.yaml"
_URL_PATH = "example.com/motion.yaml"

_FILE_YAML = "blueprint:\n  name: From Disk\n  domain: automation\n"
_URL_YAML = "blueprint:\n  name: From Source URL\n  domain: automation\n"


@pytest.fixture(autouse=True)
def _no_embedded_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every case to "not embedded" so the direct tier opts in."""
    monkeypatch.setattr(
        blueprint_sources, "get_embedded_config_dir", lambda: None, raising=True
    )


def _embed(monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> None:
    monkeypatch.setattr(
        blueprint_sources, "get_embedded_config_dir", lambda: str(config_dir)
    )


def _write_blueprint(config_dir: Path, domain: str, path: str, text: str) -> Path:
    target = config_dir / "blueprints" / domain / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _installed(hass: HomeAssistant, text: str, path: str = _PATH) -> None:
    """``text`` as an installed automation blueprint in Core's config directory."""
    _write_blueprint(Path(hass.config.config_dir), "automation", path, text)


@pytest.fixture
async def importer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> AiohttpClientMocker:
    """Core's blueprint importer, with the automation and script blueprint
    domains it imports into; the URLs it downloads are served here."""
    for domain in ("blueprint", "automation", "script"):
        assert await async_setup_component(hass, domain, {})
    return aioclient_mock


class Calls(NamedTuple):
    """Call-through spies on every tier below the direct read."""

    component: AsyncMock
    services: AsyncMock
    bridge: AsyncMock

    def component_reads(self) -> int:
        return sum(
            1 for c in self.component.call_args_list if c.args[0] == _BLUEPRINT_GET
        )

    def tools_reads(self) -> list[dict[str, Any]]:
        return [
            c.args[2] for c in self.services.call_args_list if c.args[1] == "read_file"
        ]

    def imports(self) -> list[dict[str, Any]]:
        return [
            c.args[0]
            for c in self.bridge.call_args_list
            if c.args[0].get("type") == "blueprint/import"
        ]


@contextmanager
def _calls(
    client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
) -> Iterator[Calls]:
    with (
        patch.object(socket, "send_command", wraps=socket.send_command) as component,
        patch.object(client, "call_service", wraps=client.call_service) as services,
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as bridge,
    ):
        yield Calls(component, services, bridge)


async def _resolve(
    client: HomeAssistantClient, path: str = _PATH, source_url: str | None = None
) -> Any:
    return await resolve_blueprint_source(
        client, "automation", path, source_url=source_url
    )


# --------------------------------------------------------------- parse helper


class TestParseBlueprintBody:
    def test_input_tag_becomes_a_marker(self) -> None:
        body = parse_blueprint_body(
            "blueprint:\n"
            "  name: Motion\n"
            "trigger:\n"
            "  - entity_id: !input motion_sensor\n"
        )
        assert body is not None
        assert body["trigger"][0]["entity_id"] == {"__input__": "motion_sensor"}

    def test_secret_tag_never_resolves(self) -> None:
        """``!secret`` is neutralized to ``None``, never looked up.

        Blueprints do not use ``!secret``, but the loader is what stands between
        a hostile file and a resolved plaintext credential in a tool response.
        """
        body = parse_blueprint_body(
            "blueprint:\n  name: Sneaky\naction:\n  - token: !secret api_token\n"
        )
        assert body is not None
        assert body["action"][0] == {"token": None}

    def test_unknown_tag_is_dropped(self) -> None:
        body = parse_blueprint_body(
            "blueprint:\n  name: X\nextra: !include other.yaml\n"
        )
        assert body is not None
        assert body["extra"] is None

    def test_malformed_yaml_returns_none(self) -> None:
        assert parse_blueprint_body("blueprint: [unclosed\n") is None

    def test_non_mapping_returns_none(self) -> None:
        assert parse_blueprint_body("- just\n- a list\n") is None


# ------------------------------------------------------------ the direct jail


class TestDirectRead:
    """Nothing below the direct read answers here: no component, no tools
    entry, no source URL."""

    async def test_reads_the_real_file(
        self,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _embed(monkeypatch, tmp_path)
        _write_blueprint(tmp_path, "automation", _PATH, _FILE_YAML)

        found = await _resolve(core_client)

        assert found.text == _FILE_YAML
        assert found.source == "file"
        assert found.config == {
            "blueprint": {"name": "From Disk", "domain": "automation"}
        }

    async def test_path_is_computed_from_the_domain(
        self,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The domain picks the directory, so an automation path cannot read a
        script blueprint (and vice versa)."""
        _embed(monkeypatch, tmp_path)
        _write_blueprint(tmp_path, "script", _PATH, _FILE_YAML)

        wrong = await resolve_blueprint_source(
            core_client, "automation", _PATH, source_url=None
        )
        right = await resolve_blueprint_source(
            core_client, "script", _PATH, source_url=None
        )

        assert wrong.text is None
        assert right.text == _FILE_YAML

    @pytest.mark.parametrize(
        "evil",
        [
            "../../secrets.yaml",
            "../../../etc/passwd",
            "/etc/passwd",
            "user/../../../secrets.yaml",
            "",
        ],
    )
    async def test_escaping_paths_are_never_read(
        self,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        evil: str,
    ) -> None:
        _embed(monkeypatch, tmp_path)
        (tmp_path / "secrets.yaml").write_text("db_pw: hunter2\n", encoding="utf-8")
        (tmp_path / "blueprints" / "automation").mkdir(parents=True, exist_ok=True)

        found = await _resolve(core_client, evil)

        assert found.text is None
        assert found.config is None

    async def test_symlink_out_of_the_jail_is_refused(
        self,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Containment is checked on the RESOLVED target, not the joined path.

        A symlink that lives inside the blueprints directory but points outside
        it is the case a string-prefix check would wave through.
        """
        _embed(monkeypatch, tmp_path)
        secret = tmp_path / "secrets.yaml"
        secret.write_text("db_pw: hunter2\n", encoding="utf-8")
        base = tmp_path / "blueprints" / "automation"
        base.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(secret, base / "escape.yaml")
        except OSError, NotImplementedError, AttributeError:
            pytest.skip("symlink creation not permitted on this platform")

        found = await _resolve(core_client, "escape.yaml")

        assert found.text is None

    async def test_symlink_inside_the_jail_is_allowed(
        self,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The jail bounds the target, it does not ban symlinks outright."""
        _embed(monkeypatch, tmp_path)
        base = tmp_path / "blueprints" / "automation"
        real = _write_blueprint(tmp_path, "automation", "real.yaml", _FILE_YAML)
        try:
            os.symlink(real, base / "alias.yaml")
        except OSError, NotImplementedError, AttributeError:
            pytest.skip("symlink creation not permitted on this platform")

        found = await _resolve(core_client, "alias.yaml")

        assert found.text == _FILE_YAML

    async def test_directory_target_is_not_read(
        self,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _embed(monkeypatch, tmp_path)
        (tmp_path / "blueprints" / "automation" / "user").mkdir(parents=True)

        found = await _resolve(core_client, "user")

        assert found.text is None

    async def test_unparseable_file_still_yields_its_text(
        self,
        core_client: HomeAssistantClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A parse failure must never mask bytes that were read."""
        _embed(monkeypatch, tmp_path)
        _write_blueprint(tmp_path, "automation", _PATH, "blueprint: [unclosed\n")

        found = await _resolve(core_client)

        assert found.text == "blueprint: [unclosed\n"
        assert found.config is None
        assert found.source == "file"


# ------------------------------------------------------------- the component


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled(core_client)


class TestComponentTier:
    async def test_text_capability_serves_the_file_text_and_its_body(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        _installed(hass, _FILE_YAML)

        found = await _resolve(component_core_client)

        assert found.text == _FILE_YAML
        assert found.source == "component"
        assert found.config == {
            "blueprint": {"name": "From Disk", "domain": "automation"}
        }

    async def test_body_only_component_keeps_config_and_keeps_looking(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A component without ``blueprint_text`` (2.1.1) serves the parsed
        body only: it is kept while the ladder finds the text somewhere else."""
        older_component(monkeypatch, "blueprint_text")
        await set_up_tools_entry(hass)
        _installed(hass, _FILE_YAML)

        with _calls(core_client, socket) as calls:
            found = await _resolve(core_client)

        assert found.text == _FILE_YAML
        assert found.source == "tools_entry"
        assert found.config == {
            "blueprint": {"name": "From Disk", "domain": "automation"}
        }
        assert calls.component_reads() == 1
        assert [r["path"] for r in calls.tools_reads()] == [
            f"blueprints/automation/{_PATH}"
        ]

    async def test_capability_miss_never_sends_the_command(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        older_component(monkeypatch, "blueprint_get", "blueprint_text")
        _installed(hass, _FILE_YAML)

        with _calls(component_core_client, socket) as calls:
            found = await _resolve(component_core_client)

        assert found.source is None
        assert calls.component_reads() == 0

    async def test_unknown_command_invalidates_the_cached_caps(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A downgraded component must not keep routing to a dead command:
        Core answers ``unknown_command`` for one it no longer registers."""
        _installed(hass, _FILE_YAML)
        monkeypatch.delitem(hass.data["websocket_api"], _BLUEPRINT_GET)
        invalidated = MagicMock(wraps=blueprint_sources.invalidate_caps)
        monkeypatch.setattr(blueprint_sources, "invalidate_caps", invalidated)

        found = await _resolve(component_core_client)

        assert found.text is None
        invalidated.assert_called_once_with(component_core_client)

    async def test_a_late_answer_keeps_the_cached_caps(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only a downgrade invalidates. A command the component answered too
        late (or with an error other than ``unknown_command``, the same branch)
        says nothing about whether the command still exists; dropping the caps
        would re-probe a component that never stopped advertising it."""
        _installed(hass, _FILE_YAML)
        invalidated = MagicMock(wraps=blueprint_sources.invalidate_caps)
        monkeypatch.setattr(blueprint_sources, "invalidate_caps", invalidated)

        async with held(
            hass, _BLUEPRINT_GET, monkeypatch, client=component_core_client
        ):
            found = await _resolve(component_core_client)

        assert found.text is None
        invalidated.assert_not_called()

    async def test_a_transport_failure_serves_metadata_only(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The connection itself failing degrades, it does not raise, and keeps
        the capability."""
        _installed(hass, _FILE_YAML)
        drop_connection_on(hass, _BLUEPRINT_GET, socket)
        invalidated = MagicMock(wraps=blueprint_sources.invalidate_caps)
        monkeypatch.setattr(blueprint_sources, "invalidate_caps", invalidated)

        found = await _resolve(component_core_client)

        assert found.text is None
        assert found.config is None
        invalidated.assert_not_called()

    async def test_null_body_from_a_present_component_warns(
        self, component_core_client: HomeAssistantClient
    ) -> None:
        """A component that cannot read the blueprint (here: no such file)
        returns no body; metadata-only would otherwise be indistinguishable
        from no component."""
        found = await _resolve(component_core_client)

        assert found.text is None
        assert found.config is None
        assert found.warning is not None
        assert "could not be read or parsed" in found.warning

    async def test_the_warning_goes_once_a_later_tier_supplies_text(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The warning says "returning metadata only"; it must not survive a
        response that carries the file's contents, even ones that will not
        parse. A body-only component cannot parse this file, so it returns no
        body; the tools entry then reads its text."""
        older_component(monkeypatch, "blueprint_text")
        await set_up_tools_entry(hass)
        _installed(hass, "key: [unclosed")

        found = await _resolve(core_client)

        assert found.text == "key: [unclosed"
        assert found.config is None
        assert found.warning is None


# ------------------------------------------------------------- the source URL


class TestSourceUrlTier:
    """No component and no tools entry: the File & YAML Tools tier raises,
    which is a missing entry, not a failure of the blueprint, so the ladder
    falls through to the source URL."""

    async def test_refetch_is_used_as_the_last_resort(
        self,
        core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        importer: AiohttpClientMocker,
    ) -> None:
        importer.get(_SOURCE_URL, text=_URL_YAML)

        with _calls(core_client, socket) as calls:
            found = await _resolve(core_client, _URL_PATH, _SOURCE_URL)

        assert found.text == _URL_YAML
        assert found.source == "source_url"
        assert [m["url"] for m in calls.imports()] == [_SOURCE_URL]

    async def test_a_domain_switch_at_the_same_filename_is_refused(
        self, core_client: HomeAssistantClient, importer: AiohttpClientMocker
    ) -> None:
        """Blueprint paths are scoped by domain, filenames are not.

        An author who replaces an automation blueprint with a script one at
        the same URL keeps the same filename, so the filename check alone
        would hand the script's YAML back as this automation's body.
        """
        importer.get(
            _SOURCE_URL, text="blueprint:\n  name: Now A Script\n  domain: script\n"
        )

        found = await _resolve(core_client, _URL_PATH, _SOURCE_URL)

        assert found.text is None
        assert found.source is None

    async def test_filename_mismatch_is_refused(
        self, core_client: HomeAssistantClient, importer: AiohttpClientMocker
    ) -> None:
        """The URL serves a DIFFERENT blueprint than the one installed at this
        path; using it would hand the caller (or a restore) the wrong file."""
        importer.get(_SOURCE_URL, text=_URL_YAML)

        found = await _resolve(core_client, _PATH, _SOURCE_URL)

        assert found.text is None
        assert found.source is None

    @pytest.mark.parametrize(
        "path", [_URL_PATH, _PATH], ids=["accepted-warning", "refused-debug"]
    )
    async def test_log_lines_never_carry_the_urls_credentials(
        self,
        core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        importer: AiohttpClientMocker,
        caplog: pytest.LogCaptureFixture,
        path: str,
    ) -> None:
        """A recorded source_url can carry userinfo or a query-string token.

        The re-fetch sends the URL as recorded, but the warning a successful
        fallback writes, and the debug lines a refusal writes, must not
        persist those credentials in the Home Assistant log (CodeRabbit).
        """
        secret_url = "https://bob:s3cret@example.com:8443/motion.yaml?token=abc123#x"
        importer.get(secret_url, text=_URL_YAML)

        with (
            _calls(core_client, socket) as calls,
            caplog.at_level(logging.DEBUG, logger="ha_mcp.tools.blueprint_sources"),
        ):
            await _resolve(core_client, path, secret_url)

        assert [m["url"] for m in calls.imports()] == [secret_url]
        logged = "\n".join(
            r.getMessage()
            for r in caplog.records
            if r.name == blueprint_sources.__name__
        )
        assert "https://example.com:8443/motion.yaml" in logged
        for secret in ("bob", "s3cret", "token=abc123", "#x"):
            assert secret not in logged, secret

    async def test_nothing_available_returns_an_empty_source(
        self, core_client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
    ) -> None:
        """No tier answers and there is no source URL: no import is sent, and
        the empty result carries no warning (no component claimed the file)."""
        with _calls(core_client, socket) as calls:
            found = await _resolve(core_client)

        assert found.text is None
        assert found.config is None
        assert found.source is None
        assert found.warning is None
        assert calls.imports() == []


# ------------------------------------------------------------------- ordering


class TestTierOrder:
    """Every tier below the winner could answer; none is consulted."""

    async def test_every_tier_available_picks_the_direct_read(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        importer: AiohttpClientMocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The file on disk is the authoritative copy."""
        await set_up_tools_entry(hass)
        _installed(hass, _FILE_YAML, _URL_PATH)
        _embed(monkeypatch, Path(hass.config.config_dir))
        importer.get(_SOURCE_URL, text=_URL_YAML)

        with _calls(core_client, socket) as calls:
            found = await _resolve(core_client, _URL_PATH, _SOURCE_URL)

        assert found.text == _FILE_YAML
        assert found.source == "file"
        assert calls.component_reads() == 0
        assert calls.tools_reads() == []
        assert calls.imports() == []

    async def test_component_beats_tools_entry_and_source_url(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        importer: AiohttpClientMocker,
    ) -> None:
        await set_up_tools_entry(hass)
        _installed(hass, _FILE_YAML, _URL_PATH)
        importer.get(_SOURCE_URL, text=_URL_YAML)

        with _calls(core_client, socket) as calls:
            found = await _resolve(core_client, _URL_PATH, _SOURCE_URL)

        assert found.text == _FILE_YAML
        assert found.source == "component"
        assert calls.tools_reads() == []
        assert calls.imports() == []

    async def test_tools_entry_beats_source_url(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        importer: AiohttpClientMocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        older_component(monkeypatch, "blueprint_get", "blueprint_text")
        await set_up_tools_entry(hass)
        _installed(hass, _FILE_YAML, _URL_PATH)
        importer.get(_SOURCE_URL, text=_URL_YAML)

        with _calls(core_client, socket) as calls:
            found = await _resolve(core_client, _URL_PATH, _SOURCE_URL)

        assert found.text == _FILE_YAML
        assert found.source == "tools_entry"
        assert calls.imports() == []
