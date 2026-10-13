"""Settings rows state their range and restart need from the row payload.

The catalog sentences carry no ranges and no "Restart required": those facts
come from the setting metadata, so a changed range changes no translation.
"""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.settings_ui._handlers_advanced import _get_advanced_settings
from ha_mcp.settings_ui._handlers_backups import (
    _coerce_int_field,
    backup_config_fields,
)
from ha_mcp.tools.tools_dev import DevTools

from ._js_harness import run_script
from .test_settings_ui_js_behavior import (
    DEFAULT_FETCHES,
    MIN_DOM,
    _assert_clean_init,
)
from .test_settings_ui_js_behavior import settings_script as settings_script


def test_advanced_row_states_range_and_restart_and_reaches_its_off_value(
    settings_script: str,
) -> None:
    """Without this a user would not see the accepted values or that a save
    needs a restart, and an off value below the range could not be typed."""
    row = {
        "env_var": "X",
        "type": "int",
        "section": "search",
        "origin": "default",
        "editable": True,
        "restart_required": True,
    }
    fields = [
        {**row, "field": "fuzzy_threshold", "value": 70, "min": 0, "max": 100},
        {
            **row,
            "field": "sidecar_pin_port",
            "value": 0,
            "min": 1024,
            "max": 65535,
            "off_value": 0,
        },
    ]
    result = run_script(
        settings_script,
        initial_html=MIN_DOM,
        fetch_map={
            **DEFAULT_FETCHES,
            "/api/settings/advanced": {"status": 200, "json": {"fields": fields}},
        },
        invoke="await new Promise(r => setTimeout(r, 200));",
    )
    _assert_clean_init(result)

    helps = re.findall(r'<div class="adv-help">([^<]*)</div>', result.dom)
    assert "Range 0–100. Restart required." in helps
    assert "Range 1024–65535, or 0. Restart required." in helps
    inputs = {
        field: " ".join(
            re.findall(rf'<input[^>]*data-adv-field="{field}"[^>]*>', result.dom)
        )
        for field in ("fuzzy_threshold", "sidecar_pin_port")
    }
    assert 'min="0"' in inputs["fuzzy_threshold"]
    assert 'max="100"' in inputs["fuzzy_threshold"]
    assert 'min="0"' in inputs["sidecar_pin_port"]


@pytest.mark.parametrize(
    ("endpoint", "payload", "invoke", "help_re", "expected"),
    [
        (
            "/api/settings/backup-config",
            {
                "fields": [
                    {
                        "field": "auto_backup_throttle_minutes",
                        "env_var": "AUTO_BACKUP_THROTTLE_MINUTES",
                        "value": 5,
                        "origin": "file",
                        "editable": True,
                        "min": 0,
                        "max": 1440,
                    }
                ],
                "is_addon": False,
            },
            "await loadBackupConfig(); await new Promise(r => setTimeout(r, 100));",
            r'<span class="backup-field-help">([^<]*)</span>',
            "Range 0–1440.",
        ),
        (
            "/api/settings/features",
            {
                "flags": {
                    "tool_search_max_results": {
                        "value": 5,
                        "origin": "default",
                        "editable": True,
                        "type": "int",
                        "env_var": "TOOL_SEARCH_MAX_RESULTS",
                        "min": 2,
                        "max": 10,
                    }
                },
                "beta_sub_flags": [],
                "is_addon": False,
            },
            "await new Promise(r => setTimeout(r, 200));",
            r'<div class="feature-help">([^<]*)</div>',
            "Range 2–10.",
        ),
    ],
    ids=["backup", "feature"],
)
def test_number_row_help_states_its_range(
    settings_script: str,
    endpoint: str,
    payload: dict[str, Any],
    invoke: str,
    help_re: str,
    expected: str,
) -> None:
    """Backup and feature rows have no "Range" sentence either; without the
    payload's range the user never sees the accepted values."""
    result = run_script(
        settings_script,
        initial_html=MIN_DOM,
        fetch_map={**DEFAULT_FETCHES, endpoint: {"status": 200, "json": payload}},
        invoke=invoke,
    )
    _assert_clean_init(result)
    helps = re.findall(help_re, result.dom)
    assert any(expected in h for h in helps), helps


def test_backup_rows_show_the_bounds_the_save_enforces() -> None:
    """A numeric backup row without bounds shows no range, and bounds the
    save does not enforce would show the user a wrong one."""
    numeric = [
        row
        for row in backup_config_fields()
        if isinstance(row["value"], int) and not isinstance(row["value"], bool)
    ]
    assert numeric
    for row in numeric:
        assert _coerce_int_field(row["field"], row["min"])[1] is None
        assert _coerce_int_field(row["field"], row["max"])[1] is None
        for outside in (row["min"] - 1, row["max"] + 1):
            assert _coerce_int_field(row["field"], outside)[1] is not None


async def _settings_page_rows() -> list[dict[str, Any]]:
    response = await _get_advanced_settings(None, MagicMock())
    rows: list[dict[str, Any]] = json.loads(bytes(response.body))["fields"]
    return rows


async def _dev_tool_rows() -> list[dict[str, Any]]:
    return DevTools(MagicMock())._settings_rows()


@pytest.mark.parametrize("rows_of", [_settings_page_rows, _dev_tool_rows])
async def test_an_off_value_is_reported_beside_the_range_not_as_its_minimum(
    rows_of: Callable[[], Awaitable[list[dict[str, Any]]]],
) -> None:
    """With the off value stored in ``min``, the port row reads "Range 0 to
    65,535" and an agent reading the dev tool takes ports 1 to 1023 as
    valid."""
    with_off_value = [row for row in await rows_of() if "off_value" in row]
    assert with_off_value
    for row in with_off_value:
        assert not row["min"] <= row["off_value"] <= row["max"], row
