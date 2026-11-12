"""Mandatory display overrides disable requests without rewriting saved states."""

import json
import re

import pytest

from ._astro_setup_wizard import _assert_clean_init
from ._js_harness import run_script
from ._settings_ui_dom import DEFAULT_FETCHES, MIN_DOM


@pytest.mark.parametrize("env_disabled", [False, True])
@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize(
    "name", ["ha_manage_backup", "ha_get_state", "ha_get_skill_guide"]
)
def test_ignored_disable_renders_enabled_and_roundtrips(
    env_disabled: bool, read_only: bool, name: str, settings_script: str
) -> None:
    result = run_script(
        settings_script,
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
