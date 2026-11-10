"""The ast-grep rules that refuse hand-written Home Assistant replies.

The stand-in rules from #3009 see a faked Core object. A tool test fakes Core
one layer out: it replaces the client and types in the reply, which those
rules never matched. These tests pin the rules that close that gap.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from ha_mcp.client.rest_client import HomeAssistantClient

_REPO = Path(__file__).resolve().parents[3]
_RULES = _REPO / "rules"
_REPLIES = "no-hand-made-core-replies"
_SHARED = "no-shared-fake-clients"
_HASS = "no-hand-made-hass"


def _ast_grep() -> str:
    found = shutil.which(
        "ast-grep", path=str(Path(sys.executable).parent)
    ) or shutil.which("ast-grep")
    assert found, "ast-grep-cli (dev group) is not installed in this environment"
    return found


def _scan(rule_file: Path, cwd: Path, target: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        [_ast_grep(), "scan", "--rule", str(rule_file), "--json=compact", target],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout.strip(), proc.stderr
    rows: list[dict[str, Any]] = json.loads(proc.stdout)
    return rows


def _flagged(rule: str, source: str, tmp_path: Path) -> bool:
    """Whether ``rule`` reports ``source`` written as a new unit test file."""
    sample = tmp_path / "tests" / "src" / "unit" / "test_sample.py"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(source, encoding="utf-8")
    rows = _scan(_RULES / f"{rule}.yml", tmp_path, "tests/src/unit/test_sample.py")
    return any(row["ruleId"] == rule for row in rows)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'client.send_websocket_message = AsyncMock(return_value={"success": True})\n',
            id="assigned-reply",
        ),
        pytest.param(
            'client.get_states.return_value = [{"entity_id": "light.x", "state": "on"}]\n',
            id="return-value",
        ),
        pytest.param(
            "client.send_websocket_message.side_effect = _dispatch\n",
            id="dispatch-function",
        ),
        pytest.param(
            'client = MagicMock(get_entity_state=AsyncMock(return_value={"state": "on"}))\n',
            id="constructor-keyword",
        ),
        pytest.param(
            "class Client:\n"
            "    async def send_websocket_message(self, message):\n"
            '        return {"success": True}\n',
            id="hand-written-client-class",
        ),
        pytest.param(
            'with patch.object(client, "_request", AsyncMock(return_value={})):\n'
            "    pass\n",
            id="patch-object",
        ),
        pytest.param(
            'with patch("ha_mcp.tools.x.get_websocket_client", AsyncMock(return_value=ws)):\n'
            "    pass\n",
            id="patched-websocket-client",
        ),
        pytest.param(
            'ws.send_command = AsyncMock(return_value={"success": False, "error": "no"})\n',
            id="typed-in-error-reply",
        ),
        pytest.param(
            "client.send_websocket_message = AsyncMock(\n"
            '    side_effect=[{"success": True}, HomeAssistantConnectionError("x")]\n'
            ")\n",
            id="reply-list-ending-in-an-error",
        ),
        pytest.param(
            "client.get_states = AsyncMock(\n"
            '    return_value=HomeAssistantConnectionError("x")\n'
            ")\n",
            id="error-object-returned-not-raised",
        ),
        pytest.param(
            "client.get_states.return_value = HomeAssistantPayload()\n",
            id="return-value-named-like-the-client",
        ),
        pytest.param(
            "client.get_states.side_effect = HomeAssistantPayloadFactory()\n",
            id="side-effect-that-is-not-an-error",
        ),
    ],
)
def test_a_typed_in_core_reply_is_refused(source: str, tmp_path: Path) -> None:
    """A new test that mocks the client's reply fails the lint."""
    assert _flagged(_REPLIES, source, tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "client.send_websocket_message = AsyncMock(\n"
            '    side_effect=HomeAssistantConnectionError("down")\n'
            ")\n",
            id="transport-failure",
        ),
        pytest.param(
            'client.get_states.side_effect = HomeAssistantCommandTimeout("slow")\n',
            id="timeout",
        ),
        pytest.param(
            "async def test_it(core_client):\n"
            '    reply = await core_client.send_websocket_message({"type": "ping"})\n'
            '    assert reply["success"]\n',
            id="real-client-call",
        ),
    ],
)
def test_a_real_reply_or_a_raised_failure_is_allowed(
    source: str, tmp_path: Path
) -> None:
    """The fixture, and a failure Core cannot give, stay usable."""
    assert not _flagged(_REPLIES, source, tmp_path)


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param(
            "from .test_tools_traces import _make_pooled_client\n",
            True,
            id="helper-from-a-test-module",
        ),
        pytest.param(
            "from ._logbook_fallback import fallback_client\n",
            True,
            id="old-fake-client-helper",
        ),
        pytest.param(
            "async def test_it(mock_client):\n    pass\n",
            True,
            id="mock-client-fixture",
        ),
        pytest.param(
            "from ._component_core import ws_call\n", False, id="stand-in-free-helper"
        ),
    ],
)
def test_a_borrowed_fake_client_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """Importing a fake client from a grandfathered file does not dodge the rule."""
    assert _flagged(_SHARED, source, tmp_path) is refused


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param("mock_hass = MagicMock()\n", True, id="renamed-hass"),
        pytest.param(
            "hass.data[DATA_TRACE_STORE] = object()\n", True, id="placeholder-store"
        ),
        pytest.param("hass.data[DOMAIN] = {}\n", False, id="own-domain-data"),
    ],
)
def test_a_stand_in_under_another_name_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """A Core stand-in is refused whatever it is called or wherever it is put."""
    assert _flagged(_HASS, source, tmp_path) is refused


def _method_lists() -> list[set[str]]:
    rule = yaml.safe_load((_RULES / f"{_REPLIES}.yml").read_text(encoding="utf-8"))
    alternatives = rule["rule"]["all"][0]["any"][:4]
    assert [alternative["kind"] for alternative in alternatives] == [
        "assignment",
        "keyword_argument",
        "function_definition",
        "call",
    ]
    lists = []
    for alternative in alternatives:
        groups = re.findall(r"\(([a-z_|]+)\)", alternative["regex"])
        method_groups = [group for group in groups if "send_websocket_message" in group]
        assert len(method_groups) == 1, alternative["regex"]
        lists.append(set(method_groups[0].split("|")))
    return lists


def test_every_client_method_is_covered_by_the_rule() -> None:
    """A method added to the client cannot be mocked without the lint seeing it."""
    public = {
        name
        for name, _ in inspect.getmembers(
            HomeAssistantClient, inspect.iscoroutinefunction
        )
        if not name.startswith("_")
    } - {"close"}

    for covered in _method_lists():
        assert public <= covered, sorted(public - covered)


@pytest.mark.parametrize("rule", [_REPLIES, _SHARED])
def test_the_grandfather_list_is_exactly_the_files_that_still_violate(
    rule: str, tmp_path: Path
) -> None:
    """A converted file leaves the list, and no clean file can be parked on it."""
    config = yaml.safe_load((_RULES / f"{rule}.yml").read_text(encoding="utf-8"))
    listed = set(config.pop("ignores"))
    config["ignores"] = []
    unlisted_rule = tmp_path / f"{rule}.yml"
    unlisted_rule.write_text(yaml.safe_dump(config), encoding="utf-8")

    violating = {
        row["file"].replace("\\", "/") for row in _scan(unlisted_rule, _REPO, "tests")
    }

    assert listed == violating, {
        "listed but clean": sorted(listed - violating),
        "violating but unlisted": sorted(violating - listed),
    }


@pytest.mark.parametrize("tree", ["tests/src/unit", "tests/addon"])
def test_the_private_access_list_is_exactly_the_files_that_still_violate(
    tree: str,
) -> None:
    """A file cleaned of private access leaves the list; none is parked on it."""
    import tomllib

    config = tomllib.loads((_REPO / tree / "ruff.toml").read_text(encoding="utf-8"))
    listed = set(config["lint"]["extend-per-file-ignores"])
    ruff = shutil.which("ruff", path=str(Path(sys.executable).parent)) or shutil.which(
        "ruff"
    )
    assert ruff, "ruff (dev group) is not installed in this environment"
    proc = subprocess.run(
        [
            ruff,
            "check",
            tree,
            "--isolated",
            "--select",
            "SLF001",
            "--output-format",
            "json",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    root = (_REPO / tree).resolve()
    violating = {
        Path(row["filename"]).resolve().relative_to(root).as_posix()
        for row in json.loads(proc.stdout)
    }

    assert listed == violating, {
        "listed but clean": sorted(listed - violating),
        "violating but unlisted": sorted(violating - listed),
    }
