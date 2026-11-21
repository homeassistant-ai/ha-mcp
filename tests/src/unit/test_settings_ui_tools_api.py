"""The Tools tab's API: ``GET`` and ``POST /api/settings/tools``.

The page lists every tool with its state and conversation-agent exposure, and
saves the whole state map back; ``DISABLED_TOOLS`` / ``PINNED_TOOLS`` and
strict best-practices mode decide which changes it may make.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import FEATURE_FLAG_FIELDS, reset_global_settings
from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.settings_ui import FEATURE_GATED_TOOLS, dump_tool_metadata_cache

from ._settings_ui_app import app_server, settings_http, with_env
from ._settings_ui_app import settings_world as settings_world

TOOLS = "/api/settings/tools"


async def _tools(server: HomeAssistantSmartMCPServer | None = None) -> dict[str, Any]:
    async with settings_http(server) as http:
        response = await http.get(TOOLS)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def _save(body: Any) -> httpx.Response:
    async with settings_http() as http:
        return await http.post(TOOLS, json=body)


def _saved(tmp_path: Path) -> dict[str, Any]:
    saved: dict[str, Any] = json.loads((tmp_path / "tool_config.json").read_text())
    return saved


class TestToolRows:
    async def test_the_retired_resource_tools_are_not_listed(
        self, monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
    ) -> None:
        """#1134 folded ha_list_resources / ha_read_resource into the skill guide."""
        body = await _tools(app_server(monkeypatch, core_client))
        names = {row["name"] for row in body["tools"]}

        assert "ha_list_resources" not in names
        assert "ha_read_resource" not in names

    async def test_a_tool_whose_feature_is_off_is_listed_with_its_switch(
        self, monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
    ) -> None:
        """Users discover a gated tool, and the flag that turns it on, from its row."""
        body = await _tools(app_server(monkeypatch, core_client))
        rows = {row["name"]: row for row in body["tools"]}

        assert rows["ha_manage_custom_tool"]["disabled_by"] == "enable_code_mode"
        assert rows["ha_manage_custom_tool"]["primary_tag"] == "System"

    def test_the_file_tools_ride_the_filesystem_flag(self) -> None:
        """The page tells the user to set ``disabled_by`` in the dev app config."""
        for name in (
            "ha_list_files",
            "ha_read_file",
            "ha_write_file",
            "ha_delete_file",
            "ha_config_get_yaml",
        ):
            assert FEATURE_GATED_TOOLS[name]["disabled_by"] == "enable_filesystem_tools"

    def test_every_gated_tool_names_a_real_feature_flag(self) -> None:
        """A typo would point the user at a setting that does not exist."""
        flags = {field for field, _env, _type in FEATURE_FLAG_FIELDS}

        for name, meta in FEATURE_GATED_TOOLS.items():
            assert meta["disabled_by"] in flags, name

    async def test_the_security_policy_row_is_not_labelled_beta(
        self, monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
    ) -> None:
        """#2148: its gate is a non-beta safety toggle, and the page picks its
        "how to enable" copy from the label."""
        body = await _tools(app_server(monkeypatch, core_client))
        rows = {row["name"]: row for row in body["tools"]}

        assert rows["ha_manage_security_policy"]["disabled_by_beta"] is False

    async def test_a_beta_gated_row_is_labelled_beta(
        self, monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
    ) -> None:
        body = await _tools(app_server(monkeypatch, core_client))
        rows = {row["name"]: row for row in body["tools"]}

        assert rows["ha_write_file"]["disabled_by_beta"] is True

    async def test_a_cached_row_without_the_beta_label_is_classified_by_its_flag(
        self,
    ) -> None:
        """The sidecar serves rows an older build cached without the label: a
        missing label must not expose a beta tool to conversation agents."""
        dump_tool_metadata_cache(
            [
                {
                    "name": "ha_beta_stub",
                    "tags": [],
                    "disabled_by": "enable_filesystem_tools",
                },
                {
                    "name": "ha_safety_stub",
                    "tags": [],
                    "disabled_by": "enable_security_policy_tool",
                },
                {
                    "name": "ha_stamped_stub",
                    "tags": [],
                    "disabled_by": "enable_filesystem_tools",
                    "disabled_by_beta": False,
                },
                {"name": "ha_plain_tool", "tags": []},
            ]
        )

        exposed = (await _tools())["llm_api"]

        assert exposed == {
            "ha_beta_stub": False,
            "ha_safety_stub": True,
            "ha_stamped_stub": True,
            "ha_plain_tool": True,
        }


class TestSaveValidation:
    @pytest.mark.parametrize("body", [[1, 2, 3], None], ids=["array", "null"])
    async def test_a_body_that_is_not_an_object_is_a_400(self, body: Any) -> None:
        """Not a 500 from ``body.get`` on a list."""
        response = await _save(body)

        assert response.status_code == 400
        assert response.json()["success"] is False

    async def test_states_that_are_not_an_object_are_a_400(self) -> None:
        response = await _save({"states": "not-a-dict"})

        assert response.status_code == 400

    async def test_an_unknown_state_is_dropped(self, tmp_path: Path) -> None:
        response = await _save(
            {"states": {"ha_good_tool": "disabled", "ha_bad_value": "not_a_state"}}
        )

        assert response.status_code == 200
        assert _saved(tmp_path)["tools"] == {"ha_good_tool": "disabled"}

    async def test_a_save_that_cannot_be_written_is_a_500(self, tmp_path: Path) -> None:
        """Otherwise the page shows "Saved" for a change that was lost."""
        (tmp_path / "tool_config.json").mkdir()

        response = await _save({"states": {"ha_good_tool": "disabled"}})

        assert response.status_code == 500
        assert response.json()["success"] is False
        assert "HA_MCP_CONFIG_DIR" in response.text

    async def test_the_save_answers_with_what_it_applied(self) -> None:
        response = await _save(
            {"states": {"ha_get_state": "pinned", "ha_search": "disabled"}}
        )

        body = response.json()
        assert body["success"] is True
        assert body["mode"] == "file"
        assert body["restart_required"] is True
        assert body["applied"] == {"ha_get_state": "pinned", "ha_search": "disabled"}
        assert "disabled" not in body
        assert "pinned" not in body

    async def test_an_llm_api_value_that_is_not_an_object_is_a_400(self) -> None:
        response = await _save({"states": {}, "llm_api": ["nope"]})

        assert response.status_code == 400
        assert "llm_api" in response.text


class TestEnvPinnedTools:
    async def test_resending_an_env_pinned_state_unchanged_saves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The page posts the whole state map, env-pinned rows included."""
        with_env(
            monkeypatch, DISABLED_TOOLS="ha_pinned_off", PINNED_TOOLS="ha_pinned_on"
        )

        response = await _save(
            {
                "states": {
                    "ha_pinned_off": "disabled",
                    "ha_pinned_on": "pinned",
                    "ha_unrelated_tool": "disabled",
                }
            }
        )

        assert response.status_code == 200, response.json()
        assert _saved(tmp_path)["tools"] == {"ha_unrelated_tool": "disabled"}

    async def test_changing_an_env_pinned_state_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_env(monkeypatch, DISABLED_TOOLS="ha_pinned_off")

        response = await _save({"states": {"ha_pinned_off": "enabled"}})

        assert response.status_code == 409
        assert "ha_pinned_off" in response.text

    async def test_the_page_is_told_which_tools_are_env_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_env(monkeypatch, DISABLED_TOOLS="ha_foo")

        body = await _tools()

        assert body["env_pinned"] == {"ha_foo": "disabled"}
        assert body["states"]["ha_foo"] == "disabled"


class TestLlmApiExposure:
    async def test_an_exposure_only_save_applies_without_a_restart(
        self, tmp_path: Path
    ) -> None:
        """#1745: the page posts back the default-pinned-padded state map it was
        given, which is no state change; only boolean overrides are kept."""
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {"ha_other": "disabled"}})
        )
        states = (await _tools())["states"]

        response = await _save(
            {"states": states, "llm_api": {"ha_manage_pipeline": False, "ha_bad": "no"}}
        )

        body = response.json()
        assert body["restart_required"] is False
        assert body["llm_api_applied"] == {"ha_manage_pipeline": False}
        assert _saved(tmp_path)["llm_api"] == {"ha_manage_pipeline": False}

    async def test_a_state_change_still_needs_a_restart(self) -> None:
        response = await _save({"states": {"ha_other": "disabled"}, "llm_api": {}})

        assert response.json()["restart_required"] is True

    async def test_a_saved_override_wins_and_a_beta_stub_starts_hidden(
        self, tmp_path: Path
    ) -> None:
        """A gated stub row carries its primary tag but never "beta": its gate
        decides that it starts hidden, as the registered tool will."""
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {}, "llm_api": {"ha_get_state": False}})
        )
        dump_tool_metadata_cache(
            [
                {"name": "ha_get_state", "tags": ["Entity"]},
                {
                    "name": "ha_config_set_yaml",
                    "tags": ["System"],
                    "disabled_by": "enable_yaml_config_editing",
                },
            ]
        )

        body = await _tools()

        assert body["llm_api_overrides"] == {"ha_get_state": False}
        assert body["llm_api"] == {"ha_get_state": False, "ha_config_set_yaml": False}

    async def test_only_the_in_process_server_offers_the_exposure_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing reads the exposure on the app, Docker or standalone servers."""
        assert (await _tools())["llm_api_available"] is False

        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")

        assert (await _tools())["llm_api_available"] is True


async def test_the_page_is_told_which_tools_read_only_mode_spares() -> None:
    """Their rows stay live while the other write tools are switched off."""
    exempt = (await _tools())["read_only_exempt"]

    assert exempt == sorted(READ_ONLY_EXEMPT_TOOLS)
    assert "ha_manage_backup" in exempt


class TestStrictModeKeepsTheSkillGuide:
    """#1886: strict best-practices mode publishes its acknowledgment key only
    through ha_get_skill_guide, so the tool may be disabled only without it."""

    @pytest.fixture
    def strict(self, tmp_path: Path) -> Any:
        def write(on: bool, **flags: bool) -> None:
            (tmp_path / "feature_flags.json").write_text(
                json.dumps({"enable_strict_mandatory_bps": on, **flags})
            )
            reset_global_settings()

        return write

    @pytest.fixture
    def skill_guide_disabled(self, tmp_path: Path) -> None:
        (tmp_path / "tool_config.json").write_text(
            json.dumps({"tools": {"ha_get_skill_guide": "disabled"}})
        )

    async def test_disabling_the_skill_guide_in_strict_mode_is_refused(
        self, strict: Any
    ) -> None:
        strict(True)

        response = await _save({"states": {"ha_get_skill_guide": "disabled"}})

        assert response.status_code == 409
        assert "ha_get_skill_guide" in response.text

    async def test_disabling_the_skill_guide_without_strict_mode_saves(
        self, strict: Any, tmp_path: Path
    ) -> None:
        strict(False)

        response = await _save({"states": {"ha_get_skill_guide": "disabled"}})

        assert response.json()["success"] is True
        assert _saved(tmp_path)["tools"]["ha_get_skill_guide"] == "disabled"

    @pytest.mark.usefixtures("skill_guide_disabled")
    async def test_an_already_saved_disable_can_be_sent_back(self, strict: Any) -> None:
        """A hand-edited file reaches this; the server drops it at startup."""
        strict(True)

        response = await _save({"states": {"ha_get_skill_guide": "disabled"}})

        assert response.json()["success"] is True

    @pytest.mark.usefixtures("skill_guide_disabled")
    async def test_turning_strict_mode_on_with_the_tool_disabled_is_refused(
        self, strict: Any
    ) -> None:
        strict(False)

        async with settings_http() as http:
            response = await http.post(
                "/api/settings/features",
                json={"flags": {"enable_strict_mandatory_bps": True}},
            )

        assert response.status_code == 409
        assert "ha_get_skill_guide" in response.text

    @pytest.mark.usefixtures("skill_guide_disabled")
    async def test_turning_the_parent_on_with_strict_mode_set_is_refused(
        self, strict: Any
    ) -> None:
        """Strict mode saved on under a parent saved off becomes live once the
        parent is turned on."""
        strict(True, enable_mandatory_bps=False)

        async with settings_http() as http:
            response = await http.post(
                "/api/settings/features", json={"flags": {"enable_mandatory_bps": True}}
            )

        assert response.status_code == 409
        assert "ha_get_skill_guide" in response.text

    async def test_turning_strict_mode_on_with_the_tool_enabled_saves(
        self, strict: Any
    ) -> None:
        strict(False)

        async with settings_http() as http:
            response = await http.post(
                "/api/settings/features",
                json={"flags": {"enable_strict_mandatory_bps": True}},
            )

        assert response.json()["success"] is True

    async def test_an_env_pinned_skill_guide_disable_does_not_block_strict_mode(
        self, strict: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Tools tab cannot lift it, and the server keeps the tool on in
        strict mode anyway; only a disable the user saved refuses."""
        strict(False)
        with_env(monkeypatch, DISABLED_TOOLS="ha_get_skill_guide")

        async with settings_http() as http:
            response = await http.post(
                "/api/settings/features",
                json={"flags": {"enable_strict_mandatory_bps": True}},
            )

        assert response.json()["success"] is True

    async def test_the_page_locks_the_skill_guide_row_in_strict_mode(
        self, strict: Any
    ) -> None:
        strict(True)
        assert (await _tools())["bps_locked_tools"] == ["ha_get_skill_guide"]

        strict(False)
        assert (await _tools())["bps_locked_tools"] == []
