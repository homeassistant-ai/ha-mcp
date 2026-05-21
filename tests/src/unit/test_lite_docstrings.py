"""Unit tests for LiteDocstringsTransform and the server-side wiring.

Covers three layers:

1. ``LiteDocstringsTransform`` itself — empty-mapping no-op, mapped
   replacement, unmapped passthrough, on both ``list_tools`` and
   ``get_tool`` paths.
2. ``HomeAssistantSmartMCPServer._apply_lite_docstrings`` — the gate,
   the WARNING log, the import-error fallback, and the
   ``add_transform`` failure path. Uses the ``MagicMock`` stub pattern
   from ``test_categorized_search.TestApplySearchKeywordEnrichment``.
3. The ``_LITE_DOCSTRINGS`` mapping invariant — every lite description
   names ``ha_get_skill_guide`` so the LLM still has a path to
   detailed guidance from inside the trimmed text.
"""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ha_mcp.transforms import LiteDocstringsTransform


def _make_tool(name: str, *, description: str = "") -> Tool:
    """Create a minimal Tool for testing."""

    async def noop() -> str:
        return "ok"

    return Tool.from_function(
        fn=noop,
        name=name,
        description=description,
        annotations=ToolAnnotations(readOnlyHint=True),
    )


_FULL_AUTOMATION = (
    "Get Home Assistant automation configuration.\n\n"
    "Returns the complete configuration including triggers, conditions, "
    "actions, and mode settings. (... many more paragraphs ...)"
)
_LITE_AUTOMATION = (
    "Get a Home Assistant automation. See "
    "ha_get_skill_guide for schema."
)


@pytest.fixture
def replacements() -> dict[str, str]:
    return {"ha_config_get_automation": _LITE_AUTOMATION}


@pytest.fixture
def tools() -> Sequence[Tool]:
    return [
        _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION),
        _make_tool("ha_get_state", description="Get a state."),
    ]


# ---------------------------------------------------------------------------
# Layer 1: the transform itself
# ---------------------------------------------------------------------------


class TestListTools:
    @pytest.mark.asyncio
    async def test_empty_mapping_passes_through(
        self, tools: Sequence[Tool]
    ) -> None:
        transform = LiteDocstringsTransform(replacements={})

        result = list(await transform.list_tools(tools))

        assert [t.description for t in result] == [
            _FULL_AUTOMATION,
            "Get a state.",
        ]

    @pytest.mark.asyncio
    async def test_none_mapping_passes_through(
        self, tools: Sequence[Tool]
    ) -> None:
        """``None`` replacements coerces to ``{}`` — same as the empty case."""
        transform = LiteDocstringsTransform(replacements=None)

        result = list(await transform.list_tools(tools))

        assert [t.description for t in result] == [
            _FULL_AUTOMATION,
            "Get a state.",
        ]

    @pytest.mark.asyncio
    async def test_replaces_mapped_tools_only(
        self, tools: Sequence[Tool], replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(replacements=replacements)

        result = list(await transform.list_tools(tools))

        descriptions = {t.name: t.description for t in result}
        assert descriptions["ha_config_get_automation"] == _LITE_AUTOMATION
        assert descriptions["ha_get_state"] == "Get a state."


class TestGetTool:
    @pytest.mark.asyncio
    async def test_get_tool_empty_mapping_passes_through(self) -> None:
        transform = LiteDocstringsTransform(replacements={})
        original = _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION)
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_config_get_automation", call_next)

        assert result is not None
        assert result.description == _FULL_AUTOMATION

    @pytest.mark.asyncio
    async def test_get_tool_replaces_mapped(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(replacements=replacements)
        original = _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION)
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_config_get_automation", call_next)

        assert result is not None
        assert result.description == _LITE_AUTOMATION

    @pytest.mark.asyncio
    async def test_get_tool_unmapped_passes_through(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(replacements=replacements)
        original = _make_tool("ha_get_state", description="Get a state.")
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_get_state", call_next)

        assert result is not None
        assert result.description == "Get a state."

    @pytest.mark.asyncio
    async def test_get_tool_missing_returns_none(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(replacements=replacements)
        call_next = AsyncMock(return_value=None)

        result = await transform.get_tool("ha_does_not_exist", call_next)

        assert result is None


# ---------------------------------------------------------------------------
# Layer 2: server.py wiring — mirrors TestApplySearchKeywordEnrichment
# ---------------------------------------------------------------------------


class TestApplyLiteDocstrings:
    """Tests for the server-side ``_apply_lite_docstrings`` wiring."""

    def _make_server_stub(self, *, enable_lite_docstrings: bool) -> MagicMock:
        """Minimal stub exposing only the attributes the method touches."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = MagicMock()
        stub._LITE_DOCSTRINGS = HomeAssistantSmartMCPServer._LITE_DOCSTRINGS
        stub.settings = MagicMock(
            enable_lite_docstrings=enable_lite_docstrings
        )
        stub.mcp = MagicMock()
        return stub

    def test_noop_when_flag_disabled(self) -> None:
        """When the flag is off, no transform is installed and no log."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=False)
        HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        stub.mcp.add_transform.assert_not_called()

    def test_installs_transform_when_flag_enabled(self) -> None:
        """When on, install a LiteDocstringsTransform with the real mapping."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=True)
        HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        stub.mcp.add_transform.assert_called_once()
        installed = stub.mcp.add_transform.call_args.args[0]
        assert isinstance(installed, LiteDocstringsTransform)
        assert installed._replacements is stub._LITE_DOCSTRINGS

    def test_logs_warning_when_enabled(self, caplog) -> None:
        """The trade-off WARNING must be emitted so env-var users see it."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=True)
        with caplog.at_level("WARNING"):
            HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        assert any(
            "ENABLE_LITE_DOCSTRINGS=true" in rec.message
            and "may degrade LLM performance" in rec.message
            for rec in caplog.records
        )

    def test_transform_failure_logs_second_warning(self, caplog) -> None:
        """If add_transform fails, the user must know full descs remain."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=True)
        stub.mcp.add_transform.side_effect = RuntimeError("boom")

        with caplog.at_level("WARNING"):
            HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        assert any(
            "failed to install" in rec.message
            and "full tool descriptions remain in effect" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Layer 3: the mapping invariant
# ---------------------------------------------------------------------------


class TestLiteDocstringsMappingInvariants:
    """Guard-rails on the user-visible lite descriptions themselves."""

    def test_every_lite_description_references_skill(self) -> None:
        """The design promise: every lite description points at a skill tool.

        Without this anchor, the user-facing behaviour of the toggle
        regresses to "shorter descriptions, no guidance" the moment
        someone trims an entry too aggressively.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        offenders: list[str] = []
        for name, lite in HomeAssistantSmartMCPServer._LITE_DOCSTRINGS.items():
            if "ha_get_skill_guide" not in lite:
                offenders.append(name)

        assert not offenders, (
            "Lite descriptions missing a ha_get_skill_guide pointer "
            f"(invariant from _LITE_DOCSTRINGS docstring): {offenders}"
        )

    def test_every_lite_description_starts_with_action_verb(self) -> None:
        """AGENTS.md tool-docstring rule: first word is an action verb.

        Verb list mirrors AGENTS.md > Tool Docstrings > Required for
        every tool. ``Create`` covers ``Create or update`` openers used
        on the consolidated set_* tools.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        accepted = {
            "Get",
            "List",
            "Search",
            "Create",
            "Update",
            "Delete",
            "Remove",
            "Execute",
            "Call",
            "Manage",
        }
        # Strip trailing punctuation so multi-action openers (e.g.,
        # "Update, replace, or remove ...") still validate against the
        # leading verb.
        punctuation = ",.;:"
        offenders: list[tuple[str, str]] = []
        for name, lite in HomeAssistantSmartMCPServer._LITE_DOCSTRINGS.items():
            first_word = lite.split(maxsplit=1)[0].rstrip(punctuation)
            if first_word not in accepted:
                offenders.append((name, first_word))

        assert not offenders, (
            "Lite descriptions not starting with an action verb: "
            f"{offenders}"
        )
