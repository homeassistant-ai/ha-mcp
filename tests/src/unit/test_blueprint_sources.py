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
mocked path check would prove nothing about traversal.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import blueprint_sources, component_api
from ha_mcp.tools.blueprint_sources import (
    parse_blueprint_body,
    resolve_blueprint_source,
)

from ._component_routing_helpers import make_ws, patch_ws

_PATH = "user/motion.yaml"
_SOURCE_URL = "https://example.com/motion.yaml"

_FILE_YAML = "blueprint:\n  name: From Disk\n  domain: automation\n"
_COMPONENT_YAML = "blueprint:\n  name: From Component\n  domain: automation\n"
_TOOLS_YAML = "blueprint:\n  name: From Tools Entry\n  domain: automation\n"
_URL_YAML = "blueprint:\n  name: From Source URL\n  domain: automation\n"

_CAPS_TEXT = {
    "schema_version": 1,
    "component_version": "2.1.2",
    "capabilities": ["blueprint_get", "blueprint_text"],
    "limits": {},
}
_CAPS_BODY_ONLY = {
    "schema_version": 1,
    "component_version": "2.1.1",
    "capabilities": ["blueprint_get"],
    "limits": {},
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "2.1.2",
    "capabilities": ["search"],
    "limits": {},
}


class Client:
    """Credentialed HA client spy serving only ``blueprint/import``.

    Any other frame is an ``AssertionError`` so a tier reaching for something it
    should not have is a loud failure, not a silent default.
    """

    def __init__(self, import_result: dict[str, Any] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = True
        self.import_result = import_result
        self.sent: list[dict[str, Any]] = []

    async def send_websocket_message(self, msg: dict[str, Any]) -> Any:
        self.sent.append(msg)
        if msg.get("type") == "blueprint/import":
            if self.import_result is None:
                return {"success": False, "error": "no source"}
            return {"success": True, "result": self.import_result}
        raise AssertionError(f"unexpected WebSocket frame: {msg.get('type')}")

    def frames(self, frame_type: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("type") == frame_type]


class NoCredsClient(Client):
    """A client the caps probe refuses to negotiate over (no url/token)."""

    def __init__(self, import_result: dict[str, Any] | None = None) -> None:
        super().__init__(import_result)
        self.base_url = ""
        self.token = ""


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


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


def _patch_tools_entry(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> list[dict[str, Any]]:
    """Stub the privileged ``read_file`` service the third tier calls."""
    calls: list[dict[str, Any]] = []

    async def fake_call(_client: Any, service: str, data: dict[str, Any]) -> Any:
        assert service == "read_file"
        calls.append(data)
        return response

    monkeypatch.setattr(
        "ha_mcp.tools.tools_filesystem.call_mcp_tools_service", fake_call
    )
    monkeypatch.setattr(
        "ha_mcp.tools.util_helpers.unwrap_service_response", lambda r: r
    )
    return calls


def _import_result(suggested: str, raw: str) -> dict[str, Any]:
    return {"suggested_filename": suggested, "raw_data": raw}


def _component_result(
    config: dict[str, Any] | None, text: str | None
) -> dict[str, Any]:
    return {"metadata": None, "config": config, "yaml": text}


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
    @pytest.mark.asyncio
    async def test_reads_the_real_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _embed(monkeypatch, tmp_path)
        _write_blueprint(tmp_path, "automation", _PATH, _FILE_YAML)

        found = await resolve_blueprint_source(
            Client(), "automation", _PATH, source_url=None
        )

        assert found.text == _FILE_YAML
        assert found.source == "file"
        assert found.config == {
            "blueprint": {"name": "From Disk", "domain": "automation"}
        }

    @pytest.mark.asyncio
    async def test_path_is_computed_from_the_domain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The domain picks the directory, so an automation path cannot read a
        script blueprint (and vice versa)."""
        _embed(monkeypatch, tmp_path)
        _write_blueprint(tmp_path, "script", _PATH, _FILE_YAML)

        wrong = await resolve_blueprint_source(
            NoCredsClient(), "automation", _PATH, source_url=None
        )
        right = await resolve_blueprint_source(
            NoCredsClient(), "script", _PATH, source_url=None
        )

        assert wrong.text is None
        assert right.text == _FILE_YAML

    @pytest.mark.asyncio
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evil: str
    ) -> None:
        _embed(monkeypatch, tmp_path)
        (tmp_path / "secrets.yaml").write_text("db_pw: hunter2\n", encoding="utf-8")
        (tmp_path / "blueprints" / "automation").mkdir(parents=True, exist_ok=True)

        found = await resolve_blueprint_source(
            NoCredsClient(), "automation", evil, source_url=None
        )

        assert found.text is None
        assert found.config is None

    @pytest.mark.asyncio
    async def test_symlink_out_of_the_jail_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        except (OSError, NotImplementedError, AttributeError):
            pytest.skip("symlink creation not permitted on this platform")

        found = await resolve_blueprint_source(
            NoCredsClient(), "automation", "escape.yaml", source_url=None
        )

        assert found.text is None

    @pytest.mark.asyncio
    async def test_symlink_inside_the_jail_is_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The jail bounds the target, it does not ban symlinks outright."""
        _embed(monkeypatch, tmp_path)
        base = tmp_path / "blueprints" / "automation"
        real = _write_blueprint(tmp_path, "automation", "real.yaml", _FILE_YAML)
        try:
            os.symlink(real, base / "alias.yaml")
        except (OSError, NotImplementedError, AttributeError):
            pytest.skip("symlink creation not permitted on this platform")

        found = await resolve_blueprint_source(
            NoCredsClient(), "automation", "alias.yaml", source_url=None
        )

        assert found.text == _FILE_YAML

    @pytest.mark.asyncio
    async def test_directory_target_is_not_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _embed(monkeypatch, tmp_path)
        (tmp_path / "blueprints" / "automation" / "user").mkdir(parents=True)

        found = await resolve_blueprint_source(
            NoCredsClient(), "automation", "user", source_url=None
        )

        assert found.text is None

    @pytest.mark.asyncio
    async def test_unparseable_file_still_yields_its_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parse failure must never mask bytes that were read."""
        _embed(monkeypatch, tmp_path)
        _write_blueprint(tmp_path, "automation", _PATH, "blueprint: [unclosed\n")

        found = await resolve_blueprint_source(
            NoCredsClient(), "automation", _PATH, source_url=None
        )

        assert found.text == "blueprint: [unclosed\n"
        assert found.config is None
        assert found.source == "file"


# ------------------------------------------------------------- the component


class TestComponentTier:
    @pytest.mark.asyncio
    async def test_text_capability_serves_the_file_text(self) -> None:
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_result=_component_result({"blueprint": {"name": "X"}}, _COMPONENT_YAML),
        )
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                Client(), "automation", _PATH, source_url=None
            )

        assert found.text == _COMPONENT_YAML
        assert found.source == "component"
        # The component's own parsed body wins over re-parsing the text.
        assert found.config == {"blueprint": {"name": "X"}}

    @pytest.mark.asyncio
    async def test_body_only_component_keeps_config_and_keeps_looking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 2.1.1 component has no ``blueprint_text``: its config is kept while
        the ladder finds the text somewhere else."""
        calls = _patch_tools_entry(
            monkeypatch, {"success": True, "content": _TOOLS_YAML}
        )
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_BODY_ONLY,
            cmd_result=_component_result({"blueprint": {"name": "X"}}, _COMPONENT_YAML),
        )
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                Client(), "automation", _PATH, source_url=None
            )

        assert found.text == _TOOLS_YAML
        assert found.source == "tools_entry"
        assert found.config == {"blueprint": {"name": "X"}}
        assert calls == [{"path": f"blueprints/automation/{_PATH}"}]

    @pytest.mark.asyncio
    async def test_capability_miss_never_sends_the_command(self) -> None:
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_NONE,
            cmd_result=_component_result(None, _COMPONENT_YAML),
        )
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                Client(), "automation", _PATH, source_url=None
            )

        assert found.source is None
        assert not [
            c
            for c in ws.send_command.call_args_list
            if c.args[0] == "ha_mcp_tools/blueprint_get"
        ]

    @pytest.mark.asyncio
    async def test_unknown_command_invalidates_the_cached_caps(self) -> None:
        """A downgraded component must not keep routing to a dead command."""
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
        )
        client = Client()
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                client, "automation", _PATH, source_url=None
            )

        assert found.text is None
        assert client not in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_an_ordinary_command_failure_keeps_the_cached_caps(self) -> None:
        """Only a downgrade invalidates. A transient failure must not.

        Dropping the caps on any error would make the next read re-probe a
        component that never stopped advertising the command -- the sibling
        of the unknown_command case above, and the reason that branch exists.
        """
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_exc=HomeAssistantCommandError("boom", "unknown_error"),
        )
        client = Client()
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                client, "automation", _PATH, source_url=None
            )

        assert found.text is None
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_a_command_timeout_keeps_the_cached_caps(self) -> None:
        """A timeout says nothing about whether the command still exists."""
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_exc=HomeAssistantCommandTimeout("timed out"),
        )
        client = Client()
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                client, "automation", _PATH, source_url=None
            )

        assert found.text is None
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_a_transport_failure_serves_metadata_only(self) -> None:
        """The connection itself failing degrades, it does not raise."""
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_exc=HomeAssistantConnectionError("socket gone"),
        )
        client = Client()
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                client, "automation", _PATH, source_url=None
            )

        assert found.text is None
        assert found.config is None
        assert client in component_api._CAPS_CACHE

    @pytest.mark.asyncio
    async def test_null_body_from_a_present_component_warns(self) -> None:
        """Metadata-only would otherwise be indistinguishable from no component."""
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_result=_component_result(None, None),
        )
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                Client(), "automation", _PATH, source_url=None
            )

        assert found.text is None
        assert found.config is None
        assert found.warning is not None
        assert "could not be read or parsed" in found.warning

    @pytest.mark.asyncio
    async def test_warning_is_dropped_once_a_later_tier_supplies_the_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning says "returning metadata only" — it must not survive a
        response that does carry the body."""
        _patch_tools_entry(monkeypatch, {"success": True, "content": _TOOLS_YAML})
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_result=_component_result(None, None),
        )
        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                Client(), "automation", _PATH, source_url=None
            )

        assert found.text == _TOOLS_YAML
        assert found.warning is None


# ------------------------------------------------------------- the source URL


class TestSourceUrlTier:
    @pytest.mark.asyncio
    async def test_refetch_is_used_as_the_last_resort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_tools_entry(monkeypatch, {"success": False, "error": "does not exist"})
        client = NoCredsClient(_import_result("user/motion", _URL_YAML))

        found = await resolve_blueprint_source(
            client, "automation", _PATH, source_url=_SOURCE_URL
        )

        assert found.text == _URL_YAML
        assert found.source == "source_url"
        assert client.frames("blueprint/import")[0]["url"] == _SOURCE_URL

    @pytest.mark.asyncio
    async def test_filename_mismatch_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The URL now serves a DIFFERENT blueprint — using it would hand the
        caller (or a restore) the wrong file entirely."""
        _patch_tools_entry(monkeypatch, {"success": False, "error": "does not exist"})
        client = NoCredsClient(_import_result("someone_else/other", _URL_YAML))

        found = await resolve_blueprint_source(
            client, "automation", _PATH, source_url=_SOURCE_URL
        )

        assert found.text is None
        assert found.source is None

    @pytest.mark.asyncio
    async def test_no_source_url_sends_no_import_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_tools_entry(monkeypatch, {"success": False, "error": "does not exist"})
        client = NoCredsClient()

        found = await resolve_blueprint_source(
            client, "automation", _PATH, source_url=None
        )

        assert found.text is None
        assert client.sent == []


# ------------------------------------------------------------------- ordering


class TestTierOrder:
    @pytest.mark.asyncio
    async def test_every_tier_available_picks_the_direct_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All four can answer; the file on disk is the authoritative copy."""
        _embed(monkeypatch, tmp_path)
        _write_blueprint(tmp_path, "automation", _PATH, _FILE_YAML)
        tools_calls = _patch_tools_entry(
            monkeypatch, {"success": True, "content": _TOOLS_YAML}
        )
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_result=_component_result(None, _COMPONENT_YAML),
        )
        client = Client(_import_result("user/motion", _URL_YAML))

        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                client, "automation", _PATH, source_url=_SOURCE_URL
            )

        assert found.text == _FILE_YAML
        assert found.source == "file"
        # No lower tier was even consulted.
        assert ws.send_command.await_count == 0
        assert tools_calls == []
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_component_beats_tools_entry_and_source_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools_calls = _patch_tools_entry(
            monkeypatch, {"success": True, "content": _TOOLS_YAML}
        )
        ws = make_ws(
            "ha_mcp_tools/blueprint_get",
            info_result=_CAPS_TEXT,
            cmd_result=_component_result(None, _COMPONENT_YAML),
        )
        client = Client(_import_result("user/motion", _URL_YAML))

        with patch_ws(ws, blueprint_sources):
            found = await resolve_blueprint_source(
                client, "automation", _PATH, source_url=_SOURCE_URL
            )

        assert found.text == _COMPONENT_YAML
        assert found.source == "component"
        assert tools_calls == []
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_tools_entry_beats_source_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_tools_entry(monkeypatch, {"success": True, "content": _TOOLS_YAML})
        client = NoCredsClient(_import_result("user/motion", _URL_YAML))

        found = await resolve_blueprint_source(
            client, "automation", _PATH, source_url=_SOURCE_URL
        )

        assert found.text == _TOOLS_YAML
        assert found.source == "tools_entry"
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_nothing_available_returns_an_empty_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_tools_entry(monkeypatch, {"success": False, "error": "does not exist"})

        found = await resolve_blueprint_source(
            NoCredsClient(), "automation", _PATH, source_url=None
        )

        assert found.text is None
        assert found.config is None
        assert found.source is None
        assert found.warning is None

    @pytest.mark.asyncio
    async def test_a_raising_tools_entry_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The File & YAML Tools gates raise rather than return; that is a
        missing entry, not a failure of the blueprint being asked for."""
        from fastmcp.exceptions import ToolError

        async def refuse(_c: Any, _s: str, _d: dict[str, Any]) -> Any:
            raise ToolError("entry not set up")

        monkeypatch.setattr(
            "ha_mcp.tools.tools_filesystem.call_mcp_tools_service", refuse
        )
        client = NoCredsClient(_import_result("user/motion", _URL_YAML))

        found = await resolve_blueprint_source(
            client, "automation", _PATH, source_url=_SOURCE_URL
        )

        assert found.text == _URL_YAML
        assert found.source == "source_url"
