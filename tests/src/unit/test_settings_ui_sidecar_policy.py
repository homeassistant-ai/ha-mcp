"""The stdio sidecar's tool policy routes owe the main server's guards.

The sidecar writes the same policy file with handlers of its own (the live
ones need an approval queue only the main server has), which is where a guard
goes missing unnoticed: a switch written here would be refused over there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ha_mcp.policy.model import Policy, Rule
from ha_mcp.policy.persistence import load_policy, save_policy

from ._settings_ui_app import settings_http
from ._settings_ui_app import settings_world as settings_world

POLICY = "/api/policy/config"


async def _put(body: dict[str, Any]) -> httpx.Response:
    async with settings_http(is_sidecar=True) as http:
        return await http.put(POLICY, json=body)


async def test_event_decisions_need_an_approval_pin() -> None:
    response = await _put({"rules": [], "event_decisions_enabled": True})

    assert response.status_code == 400
    assert response.json()["pin_required"] is True


async def test_leaving_out_the_rule_effect_cannot_drop_an_allow_list(
    tmp_path: Path,
) -> None:
    """#2540: an omitted rule_effect would read the allow list as a
    require-approval list."""
    save_policy(
        tmp_path, Policy(rule_effect="allow", rules=[Rule(tool_name="ha_get_state")])
    )

    response = await _put({"rules": [{"tool_name": "ha_get_state"}], "version": 1})

    assert response.status_code == 400
    assert "rule_effect" in response.json()["error"]
    assert load_policy(tmp_path).rule_effect == "allow"


async def test_a_save_over_a_corrupt_policy_reports_it_and_keeps_it(
    tmp_path: Path,
) -> None:
    stored = tmp_path / "tool_policy.json"
    stored.write_text("{not valid json")

    response = await _put({"rules": []})

    assert response.status_code == 500
    assert response.json()["policy_file_corrupt"] is True
    assert stored.read_text() == "{not valid json"


async def test_reading_a_corrupt_policy_reports_it(tmp_path: Path) -> None:
    (tmp_path / "tool_policy.json").write_text("{not valid json")

    async with settings_http(is_sidecar=True) as http:
        response = await http.get(POLICY)

    assert response.status_code == 500
    assert response.json()["policy_file_corrupt"] is True


async def test_a_misspelt_key_is_refused_by_name() -> None:
    response = await _put({"rules": [], "rule_efect": "allow"})

    assert response.status_code == 400
    assert "rule_efect" in response.json()["error"]
