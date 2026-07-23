"""Regression tests for #1991 — dashboard read tools carry readOnlyHint.

``ha_config_get_dashboard`` and ``ha_get_dashboard_screenshot`` both render
screenshots via the Puppet engine, which used to persist a ``settheme`` write
per user (#1909). PR #1837 dropped their ``readOnlyHint`` for that reason. With
upstream Puppet fixed and ha-mcp's theme-guard bracket disabled (#1991), both
tools are honestly read-only again. These tests pin the restored annotation so
a regression back to the #1837 state (``destructiveHint`` instead of
``readOnlyHint``) fails loudly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.tools.tools_dashboard_screenshot import DashboardScreenshotTools
from ha_mcp.transforms.categorized_search import _categorize_tool


def _annotations(method):
    """The ToolAnnotations the @tool decorator attached via __fastmcp__."""
    return method.__fastmcp__.annotations


class TestDashboardReadOnlyHints:
    def test_config_get_dashboard_is_read_only(self):
        annotations = _annotations(
            DashboardConfigTools(MagicMock()).ha_config_get_dashboard
        )
        assert annotations.readOnlyHint is True
        # Guard against re-regression to the #1837 state, which marked the
        # tool destructiveHint=False instead of read-only.
        assert annotations.destructiveHint is None

    def test_get_dashboard_screenshot_is_read_only(self):
        annotations = _annotations(
            DashboardScreenshotTools(MagicMock()).ha_get_dashboard_screenshot
        )
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is None

    def test_both_tools_categorize_as_read(self):
        """The categorized tool-search bucketing must classify both as read so
        a ``ha_call_read_tool`` invocation no longer hard-fails (#1991)."""
        get_dashboard = DashboardConfigTools(MagicMock()).ha_config_get_dashboard
        get_screenshot = DashboardScreenshotTools(
            MagicMock()
        ).ha_get_dashboard_screenshot
        assert _categorize_tool(get_dashboard.__fastmcp__) == "read"
        assert _categorize_tool(get_screenshot.__fastmcp__) == "read"
