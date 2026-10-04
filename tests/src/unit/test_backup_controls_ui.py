"""Backup permissions remain editable and explain the AI action boundaries."""

import html
import json
import re

import pytest
from starlette.requests import Request

from ._js_harness import extract_script_body, run_script
from .test_settings_ui_js_behavior import DEFAULT_FETCHES


@pytest.mark.parametrize("is_addon", [False, True])
def test_backup_controls_show_action_limits_and_save_both_values(
    is_addon: bool,
) -> None:
    from ha_mcp.settings_ui import _render_settings_html

    page = _render_settings_html(
        Request({"type": "http", "query_string": b"", "headers": []})
    )
    fields = [
        {
            "field": field,
            "value": value,
            "env_var": env,
            "editable": True,
            "origin": "addon" if is_addon else "default",
        }
        for field, env, value in (
            ("enable_snapshot_actions", "ENABLE_SNAPSHOT_ACTIONS", True),
            ("backup_read_only", "BACKUP_READ_ONLY", False),
        )
    ]
    result = run_script(
        extract_script_body(page),
        initial_html=page,
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/settings/backup-config": {
                "byMethod": {
                    "GET": {
                        "status": 200,
                        "json": {"fields": fields, "is_addon": is_addon},
                    },
                    "POST": {
                        "status": 200,
                        "json": {"success": True, "restart_required": is_addon},
                    },
                }
            },
        },
        invoke=(
            "await loadBackupConfig();"
            "const form = document.getElementById('backupConfigForm');"
            "form.setAttribute('data-rendered-help', form.textContent);"
            "document.querySelector('[data-field=enable_snapshot_actions]').checked = false;"
            "document.querySelector('[data-field=backup_read_only]').checked = true;"
            "await saveBackupConfig();"
        ),
        settle_ms=300,
    )
    assert not result.errors
    rendered = re.search(r'data-rendered-help="([^"]*)"', result.dom)
    assert rendered is not None
    help_text = html.unescape(rendered[1])
    assert "Allow full HA snapshot actions" in help_text
    assert "Make backup management read-only" in help_text
    assert "including listing" in help_text
    assert "restore (including edit restores)" in help_text
    assert "Automatic pre-edit backups continue" in help_text
    assert "snapshot actions are enabled" in help_text
    requests = result.fetches_to("/api/settings/backup-config")
    saved = next(request for request in requests if request["method"] == "POST")
    assert json.loads(saved["body"]) == {
        "enable_snapshot_actions": False,
        "backup_read_only": True,
    }
    assert bool(result.broadcasts_of_type("restart-required")) is is_addon
