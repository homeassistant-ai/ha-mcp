"""``read_file`` returns what is stored, tails logs, and keeps secrets masked.

Home Assistant sets the File & YAML Tools entry up, and the tests call its
``read_file`` service the way ha-mcp does.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

import custom_components.ha_mcp_tools as comp
from custom_components.ha_mcp_tools import SERVICE_READ_FILE
from custom_components.ha_mcp_tools.yaml_rt import make_yaml
from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient

from ._component_filesystem import put
from ._file_services import FileService, make_file_service

FAULT_LOG = "home-assistant.log.fault"
LOG_SAMPLE = "l1\nlé2\n\nl4 ünïcode\nl5\n"


@pytest.fixture
async def files(hass: HomeAssistant) -> FileService:
    return await make_file_service(hass)


async def _read(files: FileService, path: str, **fields: Any) -> dict[str, Any]:
    return await files(SERVICE_READ_FILE, {"path": path, **fields})


class TestLogTail:
    @pytest.mark.parametrize("tail", [1, 2, 3, 6, 7, 50])
    async def test_keeps_the_last_lines_and_counts_all_of_them(
        self, hass: HomeAssistant, files: FileService, tail: int
    ) -> None:
        await put(hass, {FAULT_LOG: LOG_SAMPLE})
        lines = LOG_SAMPLE.split("\n")

        result = await _read(files, FAULT_LOG, tail_lines=tail)

        assert result["success"] is True
        assert result["content"] == "\n".join(lines[-tail:])
        assert result["total_lines"] == len(lines)
        assert result["lines_returned"] == min(len(lines), tail)
        assert result["truncated"] is (len(lines) > tail)
        assert result["size"] == len(LOG_SAMPLE.encode())

    @pytest.mark.parametrize("shift", ["", "x"])
    async def test_a_large_log_is_never_cut_inside_a_character(
        self, hass: HomeAssistant, files: FileService, shift: str
    ) -> None:
        """The tail is read back from the end in blocks, so a block can start
        in the middle of a two-byte character; ``shift`` moves it to either
        side of one."""
        await put(hass, {FAULT_LOG: shift + "é" * 90_000 + "\ntail one\ntail two\n"})

        result = await _read(files, FAULT_LOG, tail_lines=2)

        assert result["content"] == "tail two\n"
        assert result["total_lines"] == 4
        assert result["truncated"] is True

    async def test_the_whole_file_is_never_read_at_once(
        self,
        hass: HomeAssistant,
        files: FileService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fault log grows for the life of the install."""
        content = "x\n" * 200_000
        await put(hass, {"home-assistant.log": content})
        asked: list[int] = []
        real_open = Path.open

        def spying_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            handle = real_open(path, *args, **kwargs)
            return _RecordingReader(handle, asked) if args[:1] == ("rb",) else handle

        monkeypatch.setattr(Path, "open", spying_open)

        result = await _read(files, "home-assistant.log", tail_lines=3)

        assert result["content"] == "x\nx\n"
        size = len(content)
        assert asked
        assert max(size if n < 0 else n for n in asked) < size / 2

    async def test_an_empty_log_is_one_empty_line(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {FAULT_LOG: ""})

        result = await _read(files, FAULT_LOG, tail_lines=10)

        assert (result["content"], result["size"]) == ("", 0)
        assert (result["total_lines"], result["truncated"]) == (1, False)


class _RecordingReader:
    """A binary file handle that records how much each read asked for."""

    def __init__(self, handle: Any, asked: list[int]) -> None:
        self._handle = handle
        self._asked = asked

    def __enter__(self) -> _RecordingReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()

    def read(self, size: int = -1) -> bytes:
        self._asked.append(size)
        data: bytes = self._handle.read(size)
        return data

    def seek(self, offset: int) -> int:
        position: int = self._handle.seek(offset)
        return position

    def tell(self) -> int:
        position: int = self._handle.tell()
        return position


class TestReadFile:
    async def test_returns_the_text_with_its_size_and_modified_time(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"www/x.txt": "hello world"})

        result = await _read(files, "www/x.txt")

        assert result["success"] is True
        assert result["content"] == "hello world"
        assert result["size"] == 11
        datetime.fromisoformat(result["modified"])

    @pytest.mark.parametrize(
        ("path", "error_code", "error"),
        [
            ("www/missing.txt", "not_found", "File does not exist: www/missing.txt"),
            ("www/sub", "not_a_file", "Path is not a file: www/sub"),
            (
                "home-assistant.log",
                "not_found",
                "File does not exist: home-assistant.log",
            ),
        ],
    )
    async def test_a_path_that_is_not_a_file_is_reported(
        self,
        hass: HomeAssistant,
        files: FileService,
        path: str,
        error_code: str,
        error: str,
    ) -> None:
        await put(hass, {"www/sub/inner.txt": "x"})

        assert await _read(files, path) == {
            "success": False,
            "error_code": error_code,
            "error": error,
        }

    async def test_a_folder_named_like_a_log_is_not_a_file(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"home-assistant.log/inner.txt": "x"})

        result = await _read(files, "home-assistant.log")

        assert result == {
            "success": False,
            "error_code": "not_a_file",
            "error": "Path is not a file: home-assistant.log",
        }

    async def test_a_binary_file_is_reported_not_raised(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        await put(hass, {"www/logo.png": b"\xff\xfe\xfd"})

        result = await _read(files, "www/logo.png")

        assert result["success"] is False
        assert result["error_code"] == "binary_file"
        assert "Cannot read binary file" in result["error"]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> config.Settings:
    """The server's real settings, with auto-backup on and its own directory."""
    configured = config.get_global_settings()
    for name, value in {
        "enable_auto_backup": True,
        "auto_backup_throttle_minutes": 0,
        "auto_backup_retain_per_entity": 5,
        "auto_backup_dir": str(tmp_path / "backups"),
    }.items():
        monkeypatch.setattr(configured, name, value)
    return configured


class TestBinaryFileDeleteBackup:
    """The pre-delete snapshot reads the file through the component's
    ``read_file``. For a binary file that read fails; the mandatory capture
    must skip the snapshot, or ha_delete_file refuses to delete an image in
    www/. It runs against the component's real handler, so it breaks if the
    handler's binary error wording drifts from what the capture side
    recognises."""

    async def test_mandatory_capture_skips_a_binary_file(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        settings: config.Settings,
    ) -> None:
        await make_file_service(hass)
        await put(hass, {"www/logo.png": b"\x89PNG\r\n\x1a\n\xff\xfe\x00"})
        manager = bm.get_backup_manager(core_client, settings)

        captured = await manager.maybe_snapshot(
            "file", "www/logo.png", mandatory=True, force=True
        )

        assert captured is None

    async def test_mandatory_capture_keeps_a_text_file(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        settings: config.Settings,
    ) -> None:
        await make_file_service(hass)
        await put(hass, {"www/style.css": "body {}\n"})
        manager = bm.get_backup_manager(core_client, settings)

        captured = await manager.maybe_snapshot(
            "file", "www/style.css", mandatory=True, force=True
        )

        assert captured is not None


class TestSecretsMasking:
    """secrets.yaml is returned as its key names only."""

    @pytest.mark.parametrize(
        ("secrets", "masked"),
        [
            pytest.param(
                "api_key: supersecretapikey123\npassword: 'mypassword'\n"
                'token: "abc123xyz"\n',
                'api_key: "[MASKED]"\npassword: "[MASKED]"\ntoken: "[MASKED]"',
                id="plain and quoted values",
            ),
            pytest.param(
                "\n# comment about the API key\napi_key: secret123\n\n"
                "password: pass456\n",
                'api_key: "[MASKED]"\npassword: "[MASKED]"',
                id="comments and blank lines are dropped, as a pasted secret may sit in one",
            ),
            pytest.param(
                "outer:\n  inner_secret: value\n",
                'outer: "[MASKED]"',
                id="a nested mapping is hidden whole",
            ),
            pytest.param(
                "backup_ssh_key: |\n"
                "  -----BEGIN OPENSSH PRIVATE KEY-----\n"
                "  b3BlbnNzaC1rZXktdjEAAAAA\n"
                "  -----END OPENSSH PRIVATE KEY-----\n"
                "api_password: hunter2\n",
                'backup_ssh_key: "[MASKED]"\napi_password: "[MASKED]"',
                id="a block scalar leaves no continuation line (GHSA-mc92-ww4q-6fg4)",
            ),
            pytest.param(
                "foo: !secret bar\n", 'foo: "[MASKED]"', id="a custom tag is masked"
            ),
            pytest.param(
                "base_token: &tok 'secret123'\nprod: *tok\ndev: *tok\n",
                'base_token: "[MASKED]"\nprod: "[MASKED]"\ndev: "[MASKED]"',
                id="an anchored secret is masked in every alias",
            ),
        ],
    )
    async def test_every_value_is_masked(
        self, hass: HomeAssistant, files: FileService, secrets: str, masked: str
    ) -> None:
        await put(hass, {"secrets.yaml": secrets})

        result = await _read(files, "secrets.yaml")

        assert result["content"] == masked

    @pytest.mark.parametrize(
        "secrets",
        [
            pytest.param("", id="empty"),
            pytest.param("- a\n- b\n", id="a list"),
            pytest.param("dup: 1\ndup: 2\n", id="duplicate keys"),
        ],
    )
    async def test_content_that_is_not_a_mapping_is_withheld(
        self, hass: HomeAssistant, files: FileService, secrets: str
    ) -> None:
        await put(hass, {"secrets.yaml": secrets})

        result = await _read(files, "secrets.yaml")

        assert "withheld" in result["content"].lower()
        assert "[MASKED]" not in result["content"]

    async def test_the_views_of_secrets_yaml_never_carry_a_secret(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """yaml_path and include_parsed read the masked text, not the file. The
        mask marker is quoted, as an unquoted ``[MASKED]`` would come back as
        the list ``["MASKED"]``."""
        await put(hass, {"secrets.yaml": "api_key: hunter2\n"})

        result = await _read(
            files, "secrets.yaml", yaml_path="api_key", include_parsed=True
        )

        assert "hunter2" not in json.dumps(result)
        assert result["parsed"] == "[MASKED]"

    async def test_masking_stays_off_the_event_loop(
        self,
        hass: HomeAssistant,
        files: FileService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first ``make_yaml()`` on a thread builds a ruamel instance whose
        plugin discovery globs site-packages, which Home Assistant reports as a
        blocking call when it runs on the loop."""
        await put(hass, {"secrets.yaml": "api_key: hunter2\n"})
        loop_thread = threading.get_ident()
        callers: list[int] = []

        def spying_make_yaml() -> Any:
            callers.append(threading.get_ident())
            return make_yaml()

        monkeypatch.setattr(comp, "make_yaml", spying_make_yaml)

        result = await _read(files, "secrets.yaml")

        assert result["content"] == 'api_key: "[MASKED]"'
        assert callers
        assert loop_thread not in callers


class TestYamlViews:
    """``yaml_path`` and ``include_parsed`` back ha-mcp's per-edit auto-backup
    (#1579) and ``ha_config_get_yaml`` (#1788). They run component-side because
    ruamel lives here."""

    async def _views(
        self,
        hass: HomeAssistant,
        files: FileService,
        text: str,
        key: str,
        **fields: Any,
    ) -> dict[str, Any]:
        await put(hass, {"configuration.yaml": text})
        return await _read(files, "configuration.yaml", yaml_path=key, **fields)

    async def test_the_subtree_is_only_that_key(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        views = await self._views(
            hass,
            files,
            "rest:\n  resource: http://a\n  method: GET\ntimer: {}\n",
            "rest",
        )

        assert "resource: http://a" in views["subtree"]
        assert "timer" not in views["subtree"]
        assert "parsed" not in views

    async def test_comments_and_ha_tags_are_kept(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        views = await self._views(
            hass,
            files,
            "command_line:\n  - command: !secret cmd  # inline note\n",
            "command_line",
        )

        assert "!secret cmd" in views["subtree"]
        assert "# inline note" in views["subtree"]

    async def test_a_dotted_path_walks_down(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        views = await self._views(
            hass,
            files,
            "lovelace:\n  dashboards:\n    my-d:\n      mode: yaml\n",
            "lovelace.dashboards.my-d",
        )

        assert views["subtree"].strip() == "mode: yaml"

    @pytest.mark.parametrize(
        "text", ["a: 1\n", "- a\n- b\n"], ids=["absent key", "not a mapping"]
    )
    async def test_no_subtree_and_no_error_when_the_key_is_not_there(
        self, hass: HomeAssistant, files: FileService, text: str
    ) -> None:
        views = await self._views(hass, files, text, "nope", include_parsed=True)

        assert views["subtree"] is None
        assert views.get("parsed") is None
        assert "parse_error" not in views

    async def test_a_present_key_without_a_value_is_a_match(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """``default_config:`` is a real key with no value; it must not read as
        "not defined"."""
        views = await self._views(
            hass,
            files,
            "default_config:\nhttp:\n  x: 1\n",
            "default_config",
            include_parsed=True,
        )

        assert views["subtree"] is not None
        assert views["parsed"] is None
        assert "parse_error" not in views

    @pytest.mark.parametrize(
        ("text", "parsed"),
        [
            pytest.param(
                "rest:\n  method: GET\n  timeout: 5\n",
                {"method": "GET", "timeout": 5},
                id="structured data",
            ),
            pytest.param(
                "rest:\n  api_key: !secret alert2_api_key\n",
                {"api_key": "!secret alert2_api_key"},
                id="a secret stays in its source form, never resolved",
            ),
            pytest.param(
                "rest: !include groups.yaml\n",
                "!include groups.yaml",
                id="an include stays unresolved",
            ),
            pytest.param(
                "rest:\n  n: 1\n  f: 1.5\n  b: true\n  s: x\n  z:\n  l: [1, 2]\n",
                {"n": 1, "f": 1.5, "b": True, "s": "x", "z": None, "l": [1, 2]},
                id="plain types",
            ),
            pytest.param(
                "rest:\n  on: &flag true\n  off: *flag\n",
                {"on": True, "off": True},
                id="an anchored bool stays a bool, not 1",
            ),
            pytest.param(
                "rest:\n  hi: .inf\n  lo: -.inf\n  n: .nan\n",
                {"hi": ".inf", "lo": "-.inf", "n": ".nan"},
                id="non-finite floats come back in source form",
            ),
            pytest.param(
                "rest:\n  d: 2024-01-02\n  dt: 2024-01-02 03:04:05\n",
                {"d": "2024-01-02", "dt": "2024-01-02T03:04:05"},
                id="timestamps come back as ISO strings",
            ),
        ],
    )
    async def test_parsed_data_is_json_safe(
        self, hass: HomeAssistant, files: FileService, text: str, parsed: Any
    ) -> None:
        views = await self._views(hass, files, text, "rest", include_parsed=True)

        assert views["parsed"] == parsed
        assert json.dumps(views["parsed"], allow_nan=False)

    async def test_a_secret_stays_in_the_text_view_too(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        views = await self._views(
            hass, files, "rest:\n  api_key: !secret alert2_api_key\n", "rest"
        )

        assert "!secret alert2_api_key" in views["subtree"]

    async def test_a_broken_file_is_told_apart_from_a_missing_key(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """One broken package in a glob would otherwise read as "the key is not
        defined anywhere"."""
        views = await self._views(
            hass, files, "key: [1, 2\n", "key", include_parsed=True
        )

        assert views["subtree"] is None
        assert "parsed" not in views
        assert "not valid YAML" in views["parse_error"]

    async def test_the_parse_error_carries_a_position_but_no_file_content(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """The parser's message embeds the offending source line, which can hold
        an inline credential."""
        secret = "hunter2-should-never-surface"

        views = await self._views(
            hass, files, f"rest:\n  api_key: {secret}\n  bad: [1, 2\n", "rest"
        )

        assert secret not in views["parse_error"]
        assert "line" in views["parse_error"]

    async def test_a_tail_does_not_cut_the_text_the_key_is_looked_up_in(
        self, hass: HomeAssistant, files: FileService
    ) -> None:
        """The retained tail would usually exclude the key and be invalid YAML
        on its own."""
        views = await self._views(
            hass,
            files,
            "rest:\n  method: GET\n" + "".join(f"k{i}: {i}\n" for i in range(20)),
            "rest",
            tail_lines=3,
        )

        assert "method: GET" in views["subtree"]
