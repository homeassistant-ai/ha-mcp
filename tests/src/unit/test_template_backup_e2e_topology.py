"""The no-component E2E branch must refuse capture before any HA mutation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ..e2e.workflows.auto_backup.test_capture_and_restore import (
    TestTemplateHelperCaptureRestore as TemplateHelperScenario,
)


@pytest.mark.parametrize("template_type", ["sensor", "binary_sensor"])
@pytest.mark.parametrize("haos", [False, True], ids=["container", "haos_inaddon"])
async def test_missing_component_never_creates_or_edits_helper(
    monkeypatch: pytest.MonkeyPatch, template_type: str, haos: bool
) -> None:
    """Execute the real E2E scenario with read/capture-only protocol replies.

    Any native helper write or options-flow call is an assertion failure,
    including accidental cleanup of a helper that should never be created.
    """
    monkeypatch.setenv("E2E_NO_TOOLS_ENTRY", "1")
    monkeypatch.delenv("E2E_BACKEND", raising=False)
    monkeypatch.setenv("HAOS_TEST_MODE", "inaddon" if haos else "external")
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, params))
        if name == "ha_config_list_helpers":
            assert params == {"helper_type": "template"}
            return {
                "success": False,
                "error": {
                    "code": "COMPONENT_NOT_INSTALLED",
                    "message": "Template helpers require the ha_mcp_tools component",
                },
            }
        assert name == "ha_manage_backup", f"Unexpected native write: {name}"
        assert params["scope"] == "edits"
        assert params["domain"] == "helper_template"
        if params["action"] == "create":
            return {
                "success": False,
                "error": {
                    "code": "RESOURCE_NOT_FOUND",
                    "message": "Persisted options could not be read",
                },
            }
        assert params["action"] == "list", f"Unexpected backup action: {params}"
        return {"success": True, "data": {"backups": []}}

    mcp = AsyncMock()
    mcp.call_tool.side_effect = call_tool
    ha = AsyncMock()
    await TemplateHelperScenario().test_template_options_full_loop(
        mcp_client=mcp, ha_client=ha, template_type=template_type
    )
    assert [name for name, _ in calls] == [
        "ha_config_list_helpers",
        "ha_manage_backup",
        "ha_manage_backup",
    ]
    assert calls[1][1]["entity_id"] == calls[2][1]["entity_id"]
    assert ha.mock_calls == []
