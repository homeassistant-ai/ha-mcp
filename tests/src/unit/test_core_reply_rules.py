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
import tokenize
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

_REPO = Path(__file__).resolve().parents[3]
_RULES = _REPO / "rules"
_REPLIES = "no-hand-made-core-replies"
_SHARED = "no-shared-fake-clients"
_HASS = "no-hand-made-hass"
_SUPPRESSIONS = "no-stand-in-suppressions"
_TYPED = "no-untyped-core-fixtures"
_WS_REPLIES = "no-test-websocket-replies"
_STORE_WRITES = "no-test-registry-store-writes"


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
            "class Client:\n"
            '    get_states = AsyncMock(return_value=[{"state": "on"}])\n',
            id="client-class-attribute",
        ),
        pytest.param(
            'with patch.object(client, "_request", AsyncMock(return_value={})):\n'
            "    pass\n",
            id="patch-object",
        ),
        pytest.param(
            'mocker.patch.object(client, "get_states", return_value=[])\n',
            id="mocker-patch-object",
        ),
        pytest.param(
            'monkeypatch.setattr(client, "get_states", AsyncMock(return_value=[]))\n',
            id="monkeypatch-setattr",
        ),
        pytest.param(
            'setattr(client, "call_service", AsyncMock(return_value=[]))\n',
            id="setattr",
        ),
        pytest.param(
            'with patch("ha_mcp.client.rest_client.HomeAssistantClient.get_states") as m:\n'
            "    m.return_value = []\n",
            id="patched-method-by-path",
        ),
        pytest.param(
            'client.configure_mock(**{"get_states.return_value": []})\n',
            id="configure-mock",
        ),
        pytest.param(
            'client = MagicMock(**{"get_states.return_value": []})\n',
            id="return-value-named-in-a-dict-key",
        ),
        pytest.param(
            'with patch("ha_mcp.tools.x.get_websocket_client", AsyncMock(return_value=ws)):\n'
            "    pass\n",
            id="patched-websocket-client",
        ),
        pytest.param(
            'monkeypatch.setattr("ha_mcp.tools.x.get_websocket_client", connect)\n',
            id="monkeypatched-websocket-client",
        ),
        pytest.param(
            "client.httpx_client.request = AsyncMock(return_value=Response(200, json={}))\n",
            id="replaced-http-transport",
        ),
        pytest.param(
            "with patch.object(\n"
            '    websocket_client.websocket_manager, "get_client", AsyncMock(return_value=ws)\n'
            "):\n"
            "    pass\n",
            id="patched-websocket-manager",
        ),
        pytest.param(
            'ws.send_command = AsyncMock(return_value={"success": False, "error": "no"})\n',
            id="typed-in-error-reply",
        ),
        pytest.param(
            "ws.subscribe_events = AsyncMock(return_value=7)\n",
            id="websocket-client-method",
        ),
        pytest.param(
            "client.get_entity_state = AsyncMock(\n"
            '    side_effect=HomeAssistantAPIError("Not found", status_code=404)\n'
            ")\n",
            id="typed-in-http-error",
        ),
        pytest.param(
            "client.send_websocket_message = AsyncMock(\n"
            '    side_effect=HomeAssistantCommandError("Command failed: no", "not_found")\n'
            ")\n",
            id="typed-in-command-error",
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
            "client.get_states = AsyncMock(wraps=_fake_states)\n",
            id="spy-that-wraps-a-fake",
        ),
        pytest.param(
            "client.get_states = AsyncMock(wraps=helpers.fake_states)\n",
            id="spy-that-wraps-a-fake-in-another-module",
        ),
        pytest.param(
            "client.get_states = AsyncMock(wraps=client.get_states, return_value=[])\n",
            id="spy-whose-return-value-answers-instead",
        ),
        pytest.param(
            'with patch.object(\n    client, "get_states", wraps=client.get_states, side_effect=replies\n'
            "):\n    pass\n",
            id="spy-whose-side-effect-answers-instead",
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
            'with patch.object(client, "get_states", side_effect=OSError("reset")):\n'
            "    pass\n",
            id="patched-to-give-no-answer",
        ),
        pytest.param(
            "client.get_states = AsyncMock(wraps=client.get_states)\n",
            id="spy-that-calls-the-real-method",
        ),
        pytest.param(
            'with patch.object(client, "get_states", wraps=client.get_states) as spy:\n'
            "    pass\n",
            id="patched-spy-that-calls-the-real-method",
        ),
        pytest.param(
            "async def test_it(core_client):\n"
            '    reply = await core_client.send_websocket_message({"type": "ping"})\n'
            '    assert reply["success"]\n',
            id="real-client-call",
        ),
        pytest.param(
            "class TestStartup:\n    async def test_connection(self, core_client):\n"
            "        assert await core_client.test_connection()\n",
            id="test-named-like-a-client-method",
        ),
    ],
)
def test_a_real_reply_or_no_answer_is_allowed(source: str, tmp_path: Path) -> None:
    """The fixture, and a client that gets no answer, stay usable."""
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
            "from ..test_tools_traces import _make_pooled_client\n",
            True,
            id="helper-from-a-parent-test-module",
        ),
        pytest.param(
            "from . import test_tools_traces as traces\n",
            True,
            id="test-module-imported-whole",
        ),
        pytest.param(
            "import tests.src.unit.test_tools_traces\n",
            True,
            id="test-module-imported-by-path",
        ),
        pytest.param(
            "from tests.addon.test_webhook_proxy import make_proxy\n",
            True,
            id="helper-from-an-app-test-module",
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
        pytest.param(
            "from aiohttp import test_utils\n", False, id="library-test-utilities"
        ),
        pytest.param(
            "from tests.test_constants import TEST_TOKEN\n", False, id="constants"
        ),
        pytest.param(
            "from tests.src.e2e.haos_only.test_manage_addon_modes import _wait\n",
            False,
            id="helper-under-test-from-an-e2e-module",
        ),
    ],
)
def test_only_a_borrowed_fake_client_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """Importing a fake client from a grandfathered file does not dodge the rule."""
    assert _flagged(_SHARED, source, tmp_path) is refused


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param("mock_hass = MagicMock()\n", True, id="renamed-hass"),
        pytest.param("mock_hass = AsyncMock()\n", True, id="async-mock"),
        pytest.param("hass_mock = MagicMock()\n", True, id="suffixed-name"),
        pytest.param(
            "@pytest.fixture\ndef hass(tmp_path):\n    h = MM()\n    return h\n",
            True,
            id="own-hass-fixture",
        ),
        pytest.param(
            "fake_hass: MagicMock = MagicMock()\n", True, id="annotated-assignment"
        ),
        pytest.param(
            "class Case:\n    def setup_method(self):\n        self.hass = MagicMock()\n",
            True,
            id="attribute-on-the-test-class",
        ),
        pytest.param(
            "hass.data[DATA_TRACE_STORE] = object()\n", True, id="placeholder-store"
        ),
        pytest.param(
            "hass.data[DOMAIN][KEY] = MagicMock()\n", True, id="nested-placeholder"
        ),
        pytest.param(
            "hass.data.setdefault(DATA_TRACE_STORE, object())\n",
            True,
            id="placeholder-by-setdefault",
        ),
        pytest.param("hass.data[DOMAIN] = {}\n", False, id="own-domain-data"),
    ],
)
def test_only_a_stand_in_for_hass_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """A Core stand-in is refused under another name or put into ``hass.data``."""
    assert _flagged(_HASS, source, tmp_path) is refused


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param(
            "# ast-grep-ignore: no-hand-made-core-replies\nclient.get_states = fake\n",
            True,
            id="ast-grep-ignore",
        ),
        pytest.param("trace._state = None  # noqa: SLF001\n", True, id="noqa-slf001"),
        pytest.param("# ruff: noqa\n", True, id="file-wide-noqa"),
        pytest.param("# mypy: ignore-errors\n", True, id="mypy-ignore-errors"),
        pytest.param("entry.options = None  # type: ignore\n", True, id="bare-ignore"),
        pytest.param(
            "entry.options = None  # type: ignore[assignment]\n",
            True,
            id="ignored-core-type",
        ),
        pytest.param("x = 1  # noqa: E501\n", False, id="unrelated-noqa"),
        pytest.param(
            "tool(limit='x')  # type: ignore[arg-type]\n", False, id="wrong-argument"
        ),
    ],
)
def test_only_a_suppressed_real_core_check_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """A comment cannot switch a real-Core check off for a line or a file."""
    assert _flagged(_SUPPRESSIONS, source, tmp_path) is refused


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param("async def test_it(hass):\n    pass\n", True, id="no-annotation"),
        pytest.param(
            "async def test_it(hass: Any):\n    pass\n", True, id="annotated-any"
        ),
        pytest.param(
            "async def test_it(core_client=None):\n    pass\n",
            True,
            id="default-value",
        ),
        pytest.param(
            "@pytest.fixture\ndef zones(hass):\n    return hass\n",
            True,
            id="fixture-that-takes-a-fixture",
        ),
        pytest.param(
            "def fake_settings(hass, entity_id):\n    return None\n",
            False,
            id="helper-given-what-its-caller-passes",
        ),
        pytest.param(
            "async def test_it(entity_registry, hass: HomeAssistant):\n    pass\n",
            True,
            id="one-of-two-fixtures",
        ),
        pytest.param(
            "async def test_it(hass: HomeAssistant, core_client: HomeAssistantClient):\n"
            "    pass\n",
            False,
            id="real-types",
        ),
        pytest.param("async def test_it(tmp_path):\n    pass\n", False, id="other"),
        pytest.param("lookup = lambda hass: None\n", False, id="lambda"),
    ],
)
def test_only_an_untyped_core_fixture_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """mypy checks nothing done to a Core fixture without a real annotation."""
    assert _flagged(_TYPED, source, tmp_path) is refused


def _regexes(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "regex":
                yield value
            else:
                yield from _regexes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _regexes(value)


def _method_lists() -> list[set[str]]:
    """The client-method names in each pattern of the rule that lists them."""
    rule = yaml.safe_load((_RULES / f"{_REPLIES}.yml").read_text(encoding="utf-8"))
    lists = []
    for regex in _regexes(rule["rule"]["all"][0]):
        groups = [
            group
            for group in re.findall(r"\(([a-z_|]+)\)", regex)
            if "send_websocket_message" in group
        ]
        if groups:
            assert len(groups) == 1, regex
            lists.append(set(groups[0].split("|")))
    # Assignment, class attribute, keyword, method definition, a name in a
    # call's string, a name in a dict key.
    assert len(lists) == 6, lists
    return lists


def _public_coroutines(cls: type) -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(cls, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }


def test_every_client_method_is_covered_by_the_rule() -> None:
    """A new public async method of either client is in every pattern."""
    public = (
        _public_coroutines(HomeAssistantClient)
        | _public_coroutines(HomeAssistantWebSocketClient)
    ) - {"close", "connect", "disconnect", "ping"}

    for covered in _method_lists():
        # A test class may name a test ``test_connection``.
        missing = public - covered - {"test_connection"}
        assert not missing, sorted(missing)


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param(
            "@websocket_api.websocket_command({'type': 'knx/get_base_data'})\n"
            "@callback\ndef handler(hass, connection, msg):\n    pass\n",
            True,
            id="test-authored-command",
        ),
        pytest.param(
            "@websocket_command({'type': 'x/y'})\ndef handler(hass, connection, msg):\n"
            "    pass\n",
            True,
            id="imported-decorator",
        ),
        pytest.param(
            "def handler(hass, connection, msg):\n"
            "    connection.send_result(msg['id'], {'backups': []})\n",
            True,
            id="typed-in-result",
        ),
        pytest.param(
            "def handler(hass, connection, msg):\n"
            "    connection.send_error(msg['id'], 'not_found', 'gone')\n",
            True,
            id="typed-in-error",
        ),
        pytest.param(
            "_, schema = hass.data[websocket_api.DOMAIN]['config/entity_registry/list']\n"
            "websocket_api.async_register_command(hass, 'x/y', handler, schema)\n",
            False,
            id="own-handler-re-registered",
        ),
        pytest.param(
            "await websocket_client.send_json({'id': 1, 'type': 'x/y'})\n",
            False,
            id="client-request",
        ),
    ],
)
def test_only_a_test_written_websocket_command_or_reply_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """A test neither registers its own command nor answers on a connection;
    re-registering a command's own handler, or sending a request, is allowed."""
    assert _flagged(_WS_REPLIES, source, tmp_path) is refused


def test_no_test_writes_a_websocket_command_or_reply() -> None:
    """The rule has no grandfather list: every unit test complies."""
    rule = yaml.safe_load((_RULES / f"{_WS_REPLIES}.yml").read_text(encoding="utf-8"))
    assert "ignores" not in rule
    assert _scan(_RULES / f"{_WS_REPLIES}.yml", _REPO, "tests") == []


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param(
            "ir.async_get(hass).issues[('automation', 'x')] = issue\n",
            True,
            id="issue-placed-by-hand",
        ),
        pytest.param(
            "registry.entities[entry.entity_id] = entry\n", True, id="entity-placed"
        ),
        pytest.param(
            "registry.deleted_entities[key]: RegistryEntry = deleted\n",
            True,
            id="annotated-deleted-entity",
        ),
        pytest.param(
            "del device_registry.devices[device.id]\n", True, id="device-deleted"
        ),
        pytest.param("area_registry.areas.pop(area.id)\n", True, id="area-popped"),
        pytest.param("self.labels.clear()\n", True, id="labels-cleared"),
        pytest.param(
            "ir.async_get(hass).issues.clear()\n",
            True,
            id="store-reached-through-a-call",
        ),
        pytest.param("registry.async_clear()\n", False, id="not-a-store"),
        pytest.param(
            "floor_registry.floors.update({floor.floor_id: floor})\n",
            True,
            id="floors-updated",
        ),
        pytest.param(
            "registry.categories.setdefault('automation', {})\n",
            True,
            id="category-scope-added",
        ),
        pytest.param("entry = registry.entities[entity_id]\n", False, id="entry-read"),
        pytest.param(
            "assert issue_registry.issues.get(key) is None\n", False, id="get"
        ),
        pytest.param(
            "assert registry.entities[a] == registry.entities[b]\n",
            False,
            id="comparison",
        ),
        pytest.param(
            "result['entities'][0] = row\n", False, id="reply-dict-not-a-registry"
        ),
        pytest.param("hass.data['x'] = {}\n", False, id="other-subscript"),
        pytest.param(
            "entity_registry.async_remove(entry.entity_id)\n",
            False,
            id="core-removes-it",
        ),
    ],
)
def test_only_a_write_into_a_registry_store_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """A test changes a registry through Core, never by writing its store;
    reading the store is allowed."""
    assert _flagged(_STORE_WRITES, source, tmp_path) is refused


def test_no_test_writes_into_a_registry_store() -> None:
    """The rule has no grandfather list: every unit test complies."""
    path = _RULES / f"{_STORE_WRITES}.yml"
    assert "ignores" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    assert _scan(path, _REPO, "tests") == []


@pytest.mark.parametrize("rule", [_REPLIES, _SHARED, _HASS, _SUPPRESSIONS, _TYPED])
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


def test_no_unlisted_test_suppresses_ast_grep() -> None:
    """``ast-grep-ignore`` can hide a line from the scan itself, so read the comments."""
    config = yaml.safe_load(
        (_RULES / f"{_SUPPRESSIONS}.yml").read_text(encoding="utf-8")
    )
    listed = set(config["ignores"])

    def suppresses(path: Path) -> bool:
        with tokenize.open(path) as source:
            return any(
                token.type == tokenize.COMMENT and "ast-grep-ignore" in token.string
                for token in tokenize.generate_tokens(source.readline)
            )

    suppressing = {
        path.relative_to(_REPO).as_posix()
        for tree in ("tests/src/unit", "tests/addon")
        for path in (_REPO / tree).rglob("*.py")
        if suppresses(path)
    }

    assert suppressing <= listed, sorted(suppressing - listed)


@pytest.mark.parametrize("tree", ["tests/src/unit", "tests/addon"])
def test_the_private_access_list_is_exactly_the_files_that_still_violate(
    tree: str,
) -> None:
    """A file cleaned of private access leaves the list; none is parked on it."""
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


# SLF001 sees `module._name` but not `from module import _name`, which skips
# the public entry point the same way. Ruff's PLC2701 would see it, but it also
# refuses every private segment of a module path, so the `ha_mcp._vendor`
# imports the style guide requires would trip it, and it takes no allowlist.
# The vendored packages' `ha_mcp._vendor` path, the test trees' own helper
# modules (relative or `tests.`) and dunder names stay out of it.
# The vendored packages' import path; each segment after it is still checked.
_VENDORED_PATH = r"^ha_mcp\._vendor(\.[a-z]\w*)*$"
_PRIVATE_SEGMENT = r"(^|\.)_[A-Za-z0-9]"
_PRIVATE_IMPORTS = {
    "id": "no-private-imports",
    "language": "python",
    "rule": {
        "any": [
            {
                "all": [
                    {"kind": "import_from_statement"},
                    {
                        "not": {
                            "has": {"field": "module_name", "kind": "relative_import"}
                        }
                    },
                    {"not": {"regex": r"^from\s+(tests|__future__)\b"}},
                    {
                        "any": [
                            # from ha_mcp.x import _name (vendored packages
                            # included: a test reaches them the way the code
                            # uses them)
                            {
                                "has": {
                                    "kind": "dotted_name",
                                    "regex": "^_[A-Za-z0-9]",
                                    "stopBy": "end",
                                }
                            },
                            # from ha_mcp._internal_module import name
                            {
                                "all": [
                                    {
                                        "has": {
                                            "field": "module_name",
                                            "regex": _PRIVATE_SEGMENT,
                                        }
                                    },
                                    {
                                        "not": {
                                            "has": {
                                                "field": "module_name",
                                                "regex": _VENDORED_PATH,
                                            }
                                        }
                                    },
                                ]
                            },
                        ]
                    },
                ]
            },
            # import ha_mcp._internal_module as m
            {
                "all": [
                    {"kind": "import_statement"},
                    {"not": {"regex": r"^import\s+tests\b"}},
                    {
                        "has": {
                            "kind": "dotted_name",
                            "regex": _PRIVATE_SEGMENT,
                            "stopBy": "end",
                        }
                    },
                    {
                        "not": {
                            "has": {
                                "kind": "dotted_name",
                                "regex": _VENDORED_PATH,
                                "stopBy": "end",
                            }
                        }
                    },
                ]
            },
        ]
    },
    "files": ["tests/src/unit/**", "tests/addon/**"],
    "message": "private name imported from the code under test",
    "severity": "error",
}


def _private_imports_rule(tmp_path: Path) -> Path:
    rule = tmp_path / "no-private-imports.yml"
    rule.write_text(yaml.safe_dump(_PRIVATE_IMPORTS), encoding="utf-8")
    return rule


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        pytest.param("from ha_mcp.tools.x import _helper\n", True, id="private-name"),
        pytest.param(
            "from ha_mcp.tools.x import (\n    public,\n    _helper,\n)\n",
            True,
            id="parenthesized",
        ),
        pytest.param(
            "from ha_mcp.settings_ui import _handlers as h\n", True, id="private-module"
        ),
        pytest.param(
            "from ha_mcp._rich_compat import ensure\n", True, id="private-module-path"
        ),
        pytest.param(
            "from ha_mcp._vendor.fastmcp.exceptions import ToolError\n",
            False,
            id="vendored-path",
        ),
        pytest.param(
            "from ha_mcp._vendor.fastmcp.tools.base import _default\n",
            True,
            id="vendored-private-name",
        ),
        pytest.param(
            "import ha_mcp.settings_ui._handlers as h\n",
            True,
            id="import-private-module",
        ),
        pytest.param(
            "import ha_mcp._vendor.fastmcp\n", False, id="import-vendored-path"
        ),
        pytest.param("import ha_mcp.config\n", False, id="import-public-module"),
        pytest.param(
            "from ha_mcp._vendor.mcp.shared._request_clock import RequestClock\n",
            True,
            id="vendored-private-module",
        ),
        pytest.param(
            "import ha_mcp._vendor.mcp.shared._request_clock as clock\n",
            True,
            id="import-vendored-private-module",
        ),
        pytest.param("from ._helpers import _local\n", False, id="relative-helper"),
        pytest.param(
            "from tests.src.unit._helpers import _local\n", False, id="test-helper"
        ),
        pytest.param("from ha_mcp import __version__\n", False, id="dunder"),
        pytest.param("from ha_mcp.config import get\n", False, id="public-name"),
    ],
)
def test_private_import_rule_matches_only_private_names_of_the_code(
    source: str, flagged: bool, tmp_path: Path
) -> None:
    sample = tmp_path / "tests" / "src" / "unit" / "test_sample.py"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text(source, encoding="utf-8")

    rows = _scan(
        _private_imports_rule(tmp_path), tmp_path, "tests/src/unit/test_sample.py"
    )

    assert bool(rows) is flagged


def _grandfathered_tests() -> set[str]:
    """Every test file still on one of the real-Core lists."""
    listed: set[str] = set()
    for rule_file in _RULES.glob("*.yml"):
        rule = yaml.safe_load(rule_file.read_text(encoding="utf-8"))
        listed.update(rule.get("ignores") or [])
    for tree in ("tests/src/unit", "tests/addon"):
        lint = tomllib.loads((_REPO / tree / "ruff.toml").read_text(encoding="utf-8"))
        listed.update(
            f"{tree}/{pattern}"
            for pattern in lint["lint"].get("extend-per-file-ignores", {})
        )
    mypy = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    for override in mypy["tool"]["mypy"].get("overrides", []):
        modules = override.get("module", [])
        if override.get("ignore_errors"):
            listed.update(
                module.replace(".", "/") + ".py"
                for module in ([modules] if isinstance(modules, str) else modules)
                if module.startswith("tests.")
            )
    guard = (_REPO / "tests" / "core_stand_in_guard.py").read_text(encoding="utf-8")
    listed.update(re.findall(r'"(tests/[^"]+\.py)"', guard))
    return listed


def test_a_converted_test_imports_no_private_name(tmp_path: Path) -> None:
    """A file leaving the real-Core lists reaches the code through its public
    entry points: importing ``_name`` would skip them as ``module._name`` does."""
    violating = {
        row["file"].replace("\\", "/")
        for row in _scan(_private_imports_rule(tmp_path), _REPO, "tests")
    }

    assert violating <= _grandfathered_tests(), sorted(
        violating - _grandfathered_tests()
    )
