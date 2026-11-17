"""Settings rows state their range and restart need from the row payload.

The catalog sentences carry no ranges and no "Restart required": those facts
come from the setting metadata, so a changed range changes no translation.
"""

import re
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.settings_ui import SETTINGS_ROUTES, build_settings_handlers
from ha_mcp.tools.tools_dev import DevTools

from ._js_harness import _assert_clean_init, run_script
from ._settings_ui_dom import DEFAULT_FETCHES, MIN_DOM


def _settings_page() -> TestClient:
    """The settings page's HTTP routes as the stdio sidecar serves them."""
    handlers = build_settings_handlers(server=None)
    routes = [
        Route(path, handlers[key], methods=methods)
        for path, methods, key in SETTINGS_ROUTES
    ]
    return TestClient(Starlette(routes=routes))


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
    path = "/api/settings/backup-config"
    with _settings_page() as page:
        numeric = [
            row
            for row in page.get(path).json()["fields"]
            if isinstance(row["value"], int) and not isinstance(row["value"], bool)
        ]
        assert numeric
        for row in numeric:
            field = row["field"]
            for inside in (row["min"], row["max"]):
                saved = page.post(path, json={field: inside})
                assert saved.status_code == 200, (field, inside, saved.text)
            for outside in (row["min"] - 1, row["max"] + 1):
                refused = page.post(path, json={field: outside})
                assert refused.status_code == 400, (field, outside, refused.text)


def _assert_off_value_beside_range(rows: list[dict[str, Any]]) -> None:
    with_off_value = [row for row in rows if "off_value" in row]
    assert with_off_value
    for row in with_off_value:
        assert not row["min"] <= row["off_value"] <= row["max"], row


def test_settings_page_shows_an_off_value_beside_the_range_not_as_its_minimum() -> None:
    """With the off value stored in ``min``, the port row reads "Range 0 to
    65,535"."""
    with _settings_page() as page:
        rows = page.get("/api/settings/advanced").json()["fields"]
    _assert_off_value_beside_range(rows)


async def test_dev_tool_lists_an_off_value_beside_the_range_not_as_its_minimum(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """With the off value stored in ``min``, an agent reading the dev tool
    takes ports 1 to 1023 as valid."""
    result = await DevTools(core_client).ha_dev_manage_settings(action="list")
    _assert_off_value_beside_range(result["data"]["settings"])
