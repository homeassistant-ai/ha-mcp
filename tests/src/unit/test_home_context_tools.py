"""Home context tools and the write-result reminder, against the component's book."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools.base import ToolResult, default_serializer
from ha_mcp._vendor.mcp_types import TextContent
from ha_mcp.tools import component_home_context as client_mod
from ha_mcp.tools import home_context_hint_middleware as hint_mod
from ha_mcp.tools import tools_home_context as tools_mod
from ha_mcp.tools.component_api import ComponentCaps

# The sibling module installs the homeassistant.* stubs the component needs.
from .test_component_ws_search import _REAL_VOL  # noqa: F401

hc = importlib.import_module("custom_components.ha_mcp_tools.home_context")


class _Store:
    data: Any = None

    async def async_load(self) -> Any:
        return self.data

    async def async_save(self, data: Any) -> None:
        self.data = data


def _no_registries() -> Any:
    return SimpleNamespace(entity=None, device=None, area=None)


@pytest.fixture
def book(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The component's own book behind the tools' ``send``."""
    book = hc.HomeContextBook(_Store(), lambda: "2026-10-14T00:00:00")
    sent: list[dict[str, Any]] = []

    async def send(client: Any, command: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"type": command, **kwargs})
        if command == client_mod.WS_HOME_CONTEXT_GET:
            return await hc.read(book, _no_registries, kwargs)  # type: ignore[no-any-return]
        return await book.write(  # type: ignore[no-any-return]
            kwargs["scope"],
            kwargs["content"],
            kwargs["expected_revision"],
            kwargs.get("note"),
            kwargs.get("author"),
            None,
        )

    monkeypatch.setattr(tools_mod, "send", send)
    book.sent = sent
    return book


def _tools() -> Any:
    return tools_mod.HomeContextTools(object())


async def _manage(**kwargs: Any) -> dict[str, Any]:
    return await _tools().ha_manage_home_context(**kwargs)  # type: ignore[no-any-return]


def _ctx(name: str) -> Any:
    info = SimpleNamespace(name=name)
    return SimpleNamespace(
        session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=info))
    )


class TestEditing:
    async def test_a_malformed_scope_is_explained_before_anything_is_sent(
        self, book: Any
    ) -> None:
        with pytest.raises(ToolError, match="Invalid scope 'Kitchen'"):
            await _manage(
                action="set", scope="Kitchen", expected_revision=0, content="x"
            )
        assert book.sent == []

    async def test_append_extends_the_current_text(self, book: Any) -> None:
        await _manage(
            action="set", scope="topic:pv", expected_revision=0, content="# PV"
        )
        result = await _manage(
            action="append", scope="topic:pv", expected_revision=1, content="## Battery"
        )
        stored = await _tools().ha_get_home_context(scope="topic:pv")
        assert result["revision"] == 2
        assert stored["document"]["content"] == "# PV\n\n## Battery"

    async def test_replace_text_changes_only_the_named_passage(self, book: Any) -> None:
        await _manage(
            action="set",
            scope="home",
            expected_revision=0,
            content="min 19 °C, max 23 °C",
        )
        await _manage(
            action="replace_text",
            scope="home",
            expected_revision=1,
            find="min 19 °C",
            content="min 20 °C",
        )
        stored = await _tools().ha_get_home_context(scope="home")
        assert stored["document"]["content"] == "min 20 °C, max 23 °C"

    async def test_an_ambiguous_passage_is_not_replaced(self, book: Any) -> None:
        await _manage(action="set", scope="home", expected_revision=0, content="a a")
        with pytest.raises(ToolError, match="2 times"):
            await _manage(
                action="replace_text",
                scope="home",
                expected_revision=1,
                find="a",
                content="b",
            )
        assert (await _tools().ha_get_home_context(scope="home"))[
            "latest_revision"
        ] == 1

    async def test_an_edit_on_a_stale_revision_is_refused_with_the_current_one(
        self, book: Any
    ) -> None:
        await _manage(action="set", scope="home", expected_revision=0, content="v1")
        await _manage(action="set", scope="home", expected_revision=1, content="v2")
        with pytest.raises(ToolError) as err:
            await _manage(
                action="append", scope="home", expected_revision=1, content="x"
            )
        payload = json.loads(str(err.value))
        assert (payload["scope"], payload["latest_revision"], payload["conflict"]) == (
            "home",
            2,
            True,
        )
        assert "ha_get_home_context" in payload["error"]["suggestion"]

    async def test_a_set_on_a_stale_revision_is_refused_by_the_component(
        self, book: Any
    ) -> None:
        await _manage(action="set", scope="home", expected_revision=0, content="v1")
        with pytest.raises(ToolError, match="conflict"):
            await _manage(action="set", scope="home", expected_revision=0, content="v2")

    async def test_restore_brings_back_an_older_text_as_a_new_revision(
        self, book: Any
    ) -> None:
        await _manage(action="set", scope="home", expected_revision=0, content="good")
        await _manage(action="set", scope="home", expected_revision=1, content="bad")
        result = await _manage(
            action="restore", scope="home", expected_revision=2, revision=1
        )
        stored = await _tools().ha_get_home_context(scope="home", include_history=True)
        assert result["revision"] == 3
        assert stored["document"]["content"] == "good"
        assert stored["history"][0]["note"] == "restored revision 1"

    async def test_delete_hides_the_document_from_the_listing(self, book: Any) -> None:
        await _manage(
            action="set", scope="topic:pool", expected_revision=0, content="x"
        )
        result = await _manage(action="delete", scope="topic:pool", expected_revision=1)
        listing = await _tools().ha_get_home_context()
        assert result["deleted"] is True
        assert listing == {"success": True, "scopes": [], "count": 0}

    async def test_the_writing_client_is_recorded_as_author(self, book: Any) -> None:
        await _manage(
            action="set",
            scope="home",
            expected_revision=0,
            content="x",
            ctx=_ctx("Claude Desktop"),
        )
        stored = await _tools().ha_get_home_context(scope="home")
        assert stored["document"]["author"] == "Claude Desktop"

    @pytest.mark.parametrize("action", ["set", "append", "replace_text"])
    async def test_an_edit_without_content_is_rejected(
        self, book: Any, action: str
    ) -> None:
        with pytest.raises(ToolError, match="requires content"):
            await _manage(action=action, scope="home", expected_revision=0)
        assert book.sent == []


async def test_without_the_component_capability_the_tools_say_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = ComponentCaps(
        schema_version=1,
        component_version="2.2.1",
        capabilities=frozenset({"search"}),
        limits={},
    )
    monkeypatch.setattr(client_mod, "get_component_caps", AsyncMock(return_value=caps))
    client = SimpleNamespace(base_url="http://ha", token="t")
    with pytest.raises(ToolError) as err:
        await client_mod.send(client, client_mod.WS_HOME_CONTEXT_GET)
    assert json.loads(str(err.value))["error"]["code"] == "COMPONENT_NOT_INSTALLED"


class TestHint:
    def test_targets_inside_a_nested_config_are_found(self) -> None:
        targets = hint_mod.collect_targets(
            {
                "identifier": "automation.morning",
                "entity_id": "light.a, light.b",
                "config": {
                    "triggers": [{"entity_id": ["binary_sensor.door"]}],
                    "actions": [
                        {"action": "light.turn_on", "target": {"area_id": "hall"}}
                    ],
                },
                "note": "Turn on at 7.5 lux",
            }
        )
        assert targets == {
            "entity_ids": [
                "automation.morning",
                "light.a",
                "light.b",
                "binary_sensor.door",
                "light.turn_on",
            ],
            "area_ids": ["hall"],
        }

    def test_the_warning_reaches_both_structured_and_text_output(self) -> None:
        structured = {"success": True, "warnings": ["existing"]}
        result = ToolResult(
            content=[TextContent(type="text", text=default_serializer(structured))],
            structured_content=structured,
        )
        hint_mod.add_warning(result, "read the home context")
        assert result.structured_content["warnings"] == [
            "existing",
            "read the home context",
        ]
        assert json.loads(result.content[0].text) == result.structured_content

    async def _call(
        self, tool: str, read_only: bool, scopes: Any
    ) -> tuple[ToolResult, AsyncMock]:
        tools = [
            SimpleNamespace(
                name=tool, annotations=SimpleNamespace(read_only_hint=read_only)
            )
        ]
        middleware = hint_mod.HomeContextHintMiddleware(
            get_client=object, list_tools=AsyncMock(return_value=tools)
        )
        match = AsyncMock(return_value=scopes)
        result = ToolResult(content=[], structured_content={"success": True})
        context = SimpleNamespace(
            message=SimpleNamespace(name=tool, arguments={"entity_id": "light.desk"})
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(hint_mod, "match_scopes", match)
            await middleware.on_call_tool(context, AsyncMock(return_value=result))
        return result, match

    async def test_a_write_covered_by_a_document_names_it(self) -> None:
        result, _ = await self._call(
            "ha_set_entity", False, [{"scope": "area:office", "revision": 4}]
        )
        assert "area:office (revision 4)" in result.structured_content["warnings"][0]

    async def test_reads_are_not_checked(self) -> None:
        result, match = await self._call(
            "ha_get_state", True, [{"scope": "home", "revision": 1}]
        )
        match.assert_not_awaited()
        assert "warnings" not in result.structured_content

    async def test_an_unreachable_component_leaves_the_result_alone(self) -> None:
        result, _ = await self._call("ha_set_entity", False, None)
        assert result.structured_content == {"success": True}


@pytest.mark.parametrize(("state", "shown"), [("enabled", True), ("disabled", False)])
def test_the_instructions_only_point_at_a_served_read_tool(
    monkeypatch: pytest.MonkeyPatch, state: str, shown: bool
) -> None:
    from ha_mcp.server_tool_text import home_context_instructions
    from ha_mcp.settings_ui import _persistence

    monkeypatch.setattr(
        _persistence,
        "effective_tool_config",
        lambda: {"tools": {"ha_get_home_context": state}},
    )
    assert bool(home_context_instructions()) is shown


class _RecordingMcp:
    def __init__(self) -> None:
        self.tools: list[Any] = []
        self.middleware: list[Any] = []

    def add_tool(self, tool: Any) -> None:
        self.tools.append(tool)

    def add_middleware(self, middleware: Any) -> None:
        self.middleware.append(middleware)


@pytest.mark.parametrize("enabled", [True, False])
def test_the_switch_turns_tools_reminder_and_instructions_on_and_off(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from ha_mcp import config
    from ha_mcp.server_tool_text import home_context_instructions

    monkeypatch.setattr(
        config,
        "get_global_settings",
        lambda: SimpleNamespace(enable_home_context=enabled),
    )
    mcp = _RecordingMcp()
    tools_mod.register_home_context_tools(mcp, client=None)
    assert {getattr(t, "name", getattr(t, "__name__", None)) for t in mcp.tools} == (
        {"ha_get_home_context", "ha_manage_home_context"} if enabled else set()
    )
    assert len(mcp.middleware) == int(enabled)
    assert bool(home_context_instructions()) is enabled
