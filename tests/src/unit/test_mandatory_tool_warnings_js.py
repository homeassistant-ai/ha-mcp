"""Mandatory display overrides disable requests without rewriting saved states."""

import json
import re

import pytest

from ha_mcp.settings_ui import _SETTINGS_HTML

from ._js_harness import extract_script_body, run_script
from .test_settings_ui_js_behavior import DEFAULT_FETCHES, MIN_DOM, _assert_clean_init


@pytest.mark.parametrize("env_disabled", [False, True])
@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize(
    "name", ["ha_manage_backup", "ha_get_state", "ha_get_skill_guide"]
)
def test_ignored_disable_renders_enabled_and_roundtrips(
    env_disabled: bool, read_only: bool, name: str
) -> None:
    result = run_script(
        extract_script_body(_SETTINGS_HTML),
        initial_html=MIN_DOM,
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/settings/tools": {
                "byMethod": {
                    "GET": {
                        "status": 200,
                        "json": {
                            "tools": [
                                {
                                    "name": name,
                                    "primary_tag": "System",
                                    "category": "read",
                                }
                            ],
                            "states": {name: "disabled"},
                            "env_pinned": {name: "disabled"} if env_disabled else {},
                            "bps_locked_tools": [name]
                            if name == "ha_get_skill_guide"
                            else [],
                            "ignored_disabled_tools": [name],
                        },
                    },
                    "POST": {
                        "status": 200,
                        "json": {"success": True, "restart_required": False},
                    },
                }
            },
        },
        invoke=(
            "await new Promise(r => setTimeout(r, 250)); "
            f"readOnlyState.enabled = {json.dumps(read_only)}; "
            "render(); await saveConfig();"
        ),
    )
    _assert_clean_init(result)
    enabled = re.search(rf'<input[^>]*name="tool:{name}:enabled"[^>]*>', result.dom)
    assert enabled is not None
    assert "checked" in enabled.group()
    assert "disabled" in enabled.group()
    assert "Disable request ignored" in result.dom
    assert "1 enabled" in result.dom
    assert "0 disabled" in result.dom
    posts = [
        call
        for call in result.fetches_to("/api/settings/tools")
        if call["method"] == "POST"
    ]
    assert len(posts) == 1
    assert json.loads(posts[0]["body"])["states"][name] == "disabled"
