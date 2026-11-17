"""Best-practice skill content on the write tools' answers (issue #1182).

``build_skill_content`` assembles what every write tool's ``MandatoryBPS``
parameter delivers (set_automation / _script / _scene / _helper / _dashboard /
_yaml); its file selection, dedup and section slicing are exercised here on a
skills directory of the test's own. What a caller sees on an answer, the
opt-out hint, the warning when the skills are missing and the guidance on an
error, is exercised through ``ha_config_set_automation`` (and
``ha_config_set_yaml`` for a warning list of its own) on a real ``hass``, with
the bundled skills.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.config import reset_global_settings
from ha_mcp.tools.best_practice_checker import check_automation_config
from ha_mcp.tools.config_write_helpers import (
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    build_skill_content,
)
from ha_mcp.tools.tools_config_automations import AutomationConfigTools
from ha_mcp.tools.tools_yaml_config import YamlConfigTools

from ._backup_core import backup_settings as backup_settings
from ._config_dir_yaml import write_config_files
from ._config_writes import automation_tools, script_tools
from ._core_setup import set_up_tools_entry
from ._tool_errors import error_body
from ._tools_config_automations_core import require_vendored_skills
from ._tools_config_scenes_core import config_hash, scene, scene_tools


def _master_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's server-wide kill switch, as the environment sets it."""
    monkeypatch.setenv("ENABLE_MANDATORY_BPS", "false")
    reset_global_settings()


def _skills_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skills-vendor submodule was never checked out."""
    monkeypatch.setattr("ha_mcp.utils.skill_loader.get_skills_dir", lambda: None)


@pytest.fixture
def fake_skills_dir(tmp_path: Path) -> Path:
    """Build a fake home-assistant-best-practices skill with reference files
    structured so section-extraction tests have anchors to target."""
    skill = tmp_path / "home-assistant-best-practices"
    refs = skill / "references"
    refs.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# best practices\n")
    (refs / "automation-patterns.md").write_text(
        "# Automation Patterns\n"
        "\n"
        "Top intro prose.\n"
        "\n"
        "## Native Conditions\n"
        "\n"
        "Native conditions are validated at config load.\n"
        "\n"
        "## Trigger Types\n"
        "\n"
        "Trigger types are event-driven.\n"
    )
    (refs / "template-guidelines.md").write_text("# templates\n")
    (refs / "helper-selection.md").write_text("# helpers\n")
    return tmp_path


@pytest.fixture
def patched_get_skills_dir(
    fake_skills_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the skills root at the fake skill."""
    monkeypatch.setattr(
        "ha_mcp.utils.skill_loader.get_skills_dir", lambda: fake_skills_dir
    )


# ---------------------------------------------------------------------------
# MandatoryBPS=True path: canonical files attached
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("patched_get_skills_dir")
class TestIncludeSkillCanonical:
    def test_canonical_files_attached_when_on(self) -> None:
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        body = result["references/automation-patterns.md"]
        # Full file: every H2 section present, no slicing applied.
        assert "Native Conditions" in body
        assert "Trigger Types" in body

    def test_multiple_canonical_files(self) -> None:
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=(
                "references/automation-patterns.md",
                "references/template-guidelines.md",
            ),
            referenced_files=None,
        )
        assert set(result.keys()) == {
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        }

    def test_top_level_skill_md(self) -> None:
        """Scenes use top-level SKILL.md as their canonical doc."""
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("SKILL.md",),
            referenced_files=None,
        )
        assert result == {"SKILL.md": "# best practices\n"}


# ---------------------------------------------------------------------------
# MandatoryBPS=False path: canonical files suppressed
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("patched_get_skills_dir")
class TestIncludeSkillOff:
    def test_canonical_suppressed_when_off(self) -> None:
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {}

    def test_referenced_files_still_attach_when_off(self) -> None:
        """MandatoryBPS=False suppresses canonical defaults but BP-warning
        referenced files still ride along — the LLM needs them to fix the
        input it just submitted."""
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/template-guidelines.md"},
        )
        # Only the referenced file is attached; canonical is suppressed.
        assert set(result.keys()) == {"references/template-guidelines.md"}


# ---------------------------------------------------------------------------
# Dedup: canonical ∪ referenced
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("patched_get_skills_dir")
class TestDedup:
    def test_canonical_and_referenced_union(self) -> None:
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/template-guidelines.md"},
        )
        assert set(result.keys()) == {
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        }

    def test_overlap_collapses_to_one(self) -> None:
        """Same file in both canonical and referenced is read once."""
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/automation-patterns.md"},
        )
        assert list(result.keys()) == ["references/automation-patterns.md"]


# ---------------------------------------------------------------------------
# Degraded paths
# ---------------------------------------------------------------------------


class TestDegradedPaths:
    def test_skills_vendor_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the submodule isn't checked out, build_skill_content is a no-op."""
        _skills_missing(monkeypatch)
        result = build_skill_content(
            MandatoryBPS=True,
            # Every best-practices skill has one: only the missing root can
            # make it unresolved.
            canonical_files=("SKILL.md",),
            referenced_files=None,
        )
        assert result == {}

    @pytest.mark.usefixtures("patched_get_skills_dir")
    def test_nothing_requested(self) -> None:
        """MandatoryBPS=False with no referenced_files returns empty without I/O."""
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {}

    @pytest.mark.usefixtures("patched_get_skills_dir")
    def test_missing_canonical_file_silently_skipped(self) -> None:
        """An unknown canonical file doesn't fail the response — the others
        that resolve still come back."""
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=(
                "references/automation-patterns.md",
                "references/does-not-exist.md",
            ),
            referenced_files=None,
        )
        assert "references/automation-patterns.md" in result
        assert "references/does-not-exist.md" not in result

    @pytest.mark.usefixtures("patched_get_skills_dir")
    def test_master_switch_off_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENABLE_MANDATORY_BPS=false is the server-wide master switch —
        when off, build_skill_content returns empty regardless of the
        per-call MandatoryBPS flag OR the presence of referenced_files
        from BP warnings. Operator-controlled kill switch sits above the
        per-call agent toggle."""
        _master_switch_off(monkeypatch)

        # Per-call MandatoryBPS=True alone: still empty.
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {}, "master-off must override per-call True"

        # BP-warning auto-embed alone: still empty.
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=(),
            referenced_files={"references/automation-patterns.md#native-conditions"},
        )
        assert result == {}, "master-off must override BP-warning auto-embed"

    def test_master_off_with_vendor_missing_does_not_emit_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When master is off + vendor missing + caller wanted skills,
        attach_skill_content must NOT emit the vendor-missing warning (which
        tells the operator to run ``git submodule update --init``) — the
        suppression cause is the deliberate operator config, not a missing
        submodule."""
        _master_switch_off(monkeypatch)
        _skills_missing(monkeypatch)
        response: dict[str, Any] = {"success": True}
        attach_skill_content(
            response,
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert "skill_content" not in response
        assert "warnings" not in response, (
            "master-off must not produce the vendor-missing warning"
        )

    def test_augment_error_idempotent_on_re_raise(self) -> None:
        """The hint must not double-append when augment runs twice
        (e.g. nested try/except re-raise paths)."""
        error: dict[str, Any] = {"success": False, "error": {"suggestions": []}}
        augment_error_dict_with_skill_content(error, bp_warnings=None)
        once = list(error["error"]["suggestions"])
        augment_error_dict_with_skill_content(error, bp_warnings=None)
        assert once
        assert error["error"]["suggestions"] == once

    def test_augment_tool_error_falls_through_on_non_json_body(self) -> None:
        """When the ToolError body isn't a JSON-decodable error dict,
        return the original ToolError unchanged. Preserves the contract
        for any future raise that doesn't use ``raise_tool_error``."""
        te = ToolError("plain string body, not JSON")
        result = augment_tool_error_with_skill_content(te, bp_warnings=None)
        assert result is te

    @pytest.mark.usefixtures("patched_get_skills_dir")
    def test_settings_load_raises_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_skill_content must silently degrade to {} when the settings
        stop loading — the documented contract is "skill content is
        opportunistic; never fail the surrounding write". Without this guard,
        a settings-validation regression would propagate, the outer except in
        each tool would re-map to INTERNAL_ERROR, and the agent would retry
        an already-committed mutation."""
        # Not a boolean: loading the settings raises a pydantic ValidationError.
        monkeypatch.setenv("ENABLE_MANDATORY_BPS", "garbage")
        reset_global_settings()

        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {}

    @pytest.mark.usefixtures("patched_get_skills_dir")
    def test_settings_load_propagates_unexpected_error(self) -> None:
        """A NON-config error (programming bug: AttributeError/ImportError/
        etc.) must NOT be swallowed — the except is deliberately narrowed
        to ValidationError so real bugs surface instead of being masked on
        every write (Patch76 review; repo narrow-except convention)."""
        with (
            patch(
                "ha_mcp.config.get_global_settings",
                side_effect=AttributeError("real bug, not a config issue"),
            ),
            pytest.raises(AttributeError),
        ):
            build_skill_content(
                MandatoryBPS=True,
                canonical_files=("references/automation-patterns.md",),
                referenced_files=None,
            )

    @pytest.mark.usefixtures("patched_get_skills_dir")
    def test_trailing_hash_resolves_to_whole_file(self) -> None:
        """A trailing ``#`` with empty anchor (``"path#"``) must produce
        defined behaviour — currently resolves to the whole file under
        the original ``"path#"`` key. Pin this so a refactor doesn't
        silently change it (whichever way is chosen, callers must be
        able to rely on a stable result)."""
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=(),
            referenced_files={"references/automation-patterns.md#"},
        )
        # Either it resolves to the whole file under the trailing-hash
        # key, OR it returns empty. Both are valid pins; the test fails
        # only if behaviour becomes non-deterministic (e.g. raises).
        if result:
            assert "references/automation-patterns.md#" in result
            assert "Native Conditions" in result["references/automation-patterns.md#"]


# ---------------------------------------------------------------------------
# attach_skill_content with nothing to attach
# ---------------------------------------------------------------------------


class TestAttachNothing:
    @pytest.mark.usefixtures("patched_get_skills_dir")
    def test_nothing_requested_is_silent(self) -> None:
        """MandatoryBPS=False + no referenced_files → silent (user opted out)."""
        response: dict[str, Any] = {"success": True}
        attach_skill_content(
            response,
            MandatoryBPS=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert "skill_content" not in response
        # No content delivered → no hint either; the LLM only learns about
        # the opt-out param after it has received content to opt out of.
        assert "skill_content_hint" not in response
        assert "warnings" not in response

    def test_vendor_missing_with_opt_out_is_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caller explicitly opted out with MandatoryBPS=False — don't nag."""
        _skills_missing(monkeypatch)
        response: dict[str, Any] = {"success": True}
        attach_skill_content(
            response,
            MandatoryBPS=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert "skill_content" not in response
        assert "warnings" not in response


# ---------------------------------------------------------------------------
# Anchor extraction (reactive section-slicing, issue #1182 Q3)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("patched_get_skills_dir")
class TestAnchorExtraction:
    """Reactive auto-embed ships just the markdown section pointed to by
    the warning's #anchor, not the whole reference file."""

    def test_anchored_ref_returns_section(self) -> None:
        """An anchored referenced_file returns only that section's text."""
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=(),
            referenced_files={"references/automation-patterns.md#native-conditions"},
        )
        # Section content, not the whole file:
        body = result["references/automation-patterns.md#native-conditions"]
        assert body.startswith("## Native Conditions")
        assert "Native conditions are validated" in body
        # Stops before the next H2 — does not bleed into Trigger Types.
        assert "Trigger Types" not in body

    def test_bare_supersedes_anchored_for_same_file(self) -> None:
        """If canonical already ships the whole file, the section ref is dropped.

        Otherwise we'd ship the same content twice in different shapes —
        once whole, once sliced. Wasted bytes for the LLM.
        """
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/automation-patterns.md#native-conditions"},
        )
        assert "references/automation-patterns.md" in result
        assert "references/automation-patterns.md#native-conditions" not in result

    def test_bare_does_not_supersede_anchored_for_different_file(self) -> None:
        """Dedup only collapses bare-vs-section for the SAME file. Cross-file
        refs are independent."""
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/template-guidelines.md",),
            referenced_files={"references/automation-patterns.md#native-conditions"},
        )
        assert "references/template-guidelines.md" in result
        assert "references/automation-patterns.md#native-conditions" in result

    def test_missing_anchor_silently_skipped(self) -> None:
        """An anchor that doesn't match any heading is silently omitted —
        same contract as missing files."""
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=(),
            referenced_files={"references/automation-patterns.md#does-not-exist"},
        )
        assert result == {}


# ---------------------------------------------------------------------------
# What a write tool's answer carries, on a real hass
# ---------------------------------------------------------------------------

PLAIN: dict[str, Any] = {"alias": "Plain", "triggers": [], "actions": []}
# A template trigger the checker flags: its warning cites skill sections.
FLAGGED: dict[str, Any] = {
    "alias": "Flagged",
    "triggers": [{"platform": "template", "value_template": "{{ x == 1 }}"}],
    "actions": [],
}


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AutomationConfigTools:
    """The automation tools on a ``hass`` that keeps automations.yaml."""
    return await automation_tools(hass, core_client, monkeypatch)


async def _refused(tools: AutomationConfigTools, **kwargs: Any) -> dict[str, Any]:
    """The error body of a write the tool refuses."""
    with pytest.raises(ToolError) as refused:
        await tools.ha_config_set_automation(wait=False, **kwargs)
    return error_body(refused)


def _generic_hints(suggestions: list[str]) -> list[str]:
    """The suggestions that send the caller to the skill guide's front page."""
    return [s for s in suggestions if "ha_get_skill_guide()" in s and "SKILL.md" in s]


class TestTheWriteToolsAnswer:
    async def test_the_guidance_comes_first_with_how_to_opt_out(
        self, tools: AutomationConfigTools
    ) -> None:
        """The schema shows ``MandatoryBPS`` as a bare boolean; the hint is
        where a model learns what it does, so it comes with the content and
        leads the answer, and nothing about the skills is reported missing."""
        require_vendored_skills()

        result = await tools.ha_config_set_automation(config=PLAIN, wait=False)

        assert result["skill_content"]
        assert "MandatoryBPS=false" in result["skill_content_hint"]
        assert "warnings" not in result

    async def test_the_hint_leads_and_the_bulky_content_trails(
        self, tools: AutomationConfigTools
    ) -> None:
        """Small models read top-down: the opt-out hint must not sit under
        ~25KB of skill markdown, and the write's own fields stay near it
        (BAT on PR #1448: even Opus needed five tries to find a trailing
        hint)."""
        require_vendored_skills()

        keys = list(await tools.ha_config_set_automation(config=PLAIN, wait=False))

        # Without the component the write says its references went unchecked.
        assert "validation" in keys
        assert keys[0] == "skill_content_hint"
        assert keys[-1] == "skill_content"

    async def test_a_python_transform_keeps_its_validation_above_the_content(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """What the write found wrong must not trail ~25KB of skill markdown."""
        require_vendored_skills()
        renamed = "config['alias'] = 'Renamed'"
        automations = await automation_tools(hass, core_client, monkeypatch)
        scripts = await script_tools(hass, core_client, monkeypatch)
        scenes = await scene_tools(
            hass, core_client, monkeypatch, [scene("plain", "Plain")]
        )
        automation = await automations.ha_config_set_automation(
            config=PLAIN, MandatoryBPS=False, wait=False
        )
        await scripts.ha_config_set_script(
            script_id="plain",
            config={"alias": "Plain", "sequence": []},
            MandatoryBPS=False,
            wait=False,
        )
        # The hash a caller reads before editing.
        automation_id = automation["automation_id"]
        stored_automation = await automations.ha_config_get_automation(
            entity_id=automation_id
        )
        stored_script = await scripts.ha_config_get_script(script_id="plain")

        answers = [
            await automations.ha_config_set_automation(
                entity_id=automation_id,
                config_hash=stored_automation["config_hash"],
                python_transform=renamed,
                wait=False,
            ),
            await scripts.ha_config_set_script(
                script_id="plain",
                config_hash=stored_script["config_hash"],
                python_transform=renamed,
                wait=False,
            ),
            await scenes.ha_config_set_scene(
                scene_id="plain",
                config_hash=await config_hash(scenes, "plain"),
                python_transform="config['name'] = 'Renamed'",
                wait=False,
            ),
        ]

        for answer in answers:
            keys = list(answer)
            assert "validation" in keys
            assert keys[0] == "skill_content_hint"
            assert keys[-1] == "skill_content"

    async def test_without_the_skills_a_write_says_they_are_missing(
        self, tools: AutomationConfigTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The read tool reports a missing submodule; a write that asked for
        the content does too, instead of answering without it in silence."""
        _skills_missing(monkeypatch)

        result = await tools.ha_config_set_automation(config=PLAIN, wait=False)

        assert "skill_content" not in result
        (warning,) = result["warnings"]
        assert "git submodule update --init" in warning

    async def test_without_the_skills_a_flagged_write_says_so_with_mandatory_bps_off(
        self, tools: AutomationConfigTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sections a best-practice warning cites are attached even when
        the caller opted out of the rest; their absence is reported too."""
        _skills_missing(monkeypatch)

        result = await tools.ha_config_set_automation(
            config=FLAGGED, wait=False, MandatoryBPS=False
        )

        assert "skill_content" not in result
        assert any("git submodule update --init" in w for w in result["warnings"])

    async def test_the_missing_skills_warning_joins_the_tools_own(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        backup_settings: config.Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A YAML edit of a key with a helper equivalent already warns; the
        missing skills are reported after that warning, not instead of it."""
        monkeypatch.setattr(backup_settings, "enable_yaml_edit_confirm", False)
        await write_config_files(
            hass, {"configuration.yaml": "homeassistant:\n  name: Home\n"}
        )
        await set_up_tools_entry(hass)
        _skills_missing(monkeypatch)

        result = await YamlConfigTools(core_client).ha_config_set_yaml(
            yaml_path="template",
            action="add",
            content="- sensor:\n    - name: Doubled\n      state: '{{ 2 * 2 }}'\n",
        )

        helper_advice, missing = result["warnings"]
        assert "ha_config_set_helper" in helper_advice
        assert "git submodule update --init" in missing

    async def test_an_error_without_a_best_practice_hit_still_points_to_the_guide(
        self, tools: AutomationConfigTools
    ) -> None:
        """Most errors have no best-practice warning behind them; each still
        names the skill guide, after the error's own suggestions."""
        error = (await _refused(tools, config=PLAIN, category="ghost"))["error"]

        suggestions = error["suggestions"]
        assert _generic_hints(suggestions) == [suggestions[-1]]
        assert len(suggestions) > 1
        # The singular field mirrors the first entry for older consumers.
        assert error["suggestion"] == suggestions[0]

    async def test_an_error_with_one_suggestion_keeps_it_first(
        self, tools: AutomationConfigTools
    ) -> None:
        """An error built with one suggestion carries it only in the singular
        field; adding the guide's pointer must not push that fix out."""
        rejected = {**PLAIN, "triggers": [{"trigger": "no_such_trigger"}]}

        error = (await _refused(tools, config=rejected))["error"]

        fix, pointer = error["suggestions"]
        assert error["suggestion"] == fix
        assert _generic_hints([fix, pointer]) == [pointer]

    async def test_a_rejected_write_carries_the_sections_its_warning_cites(
        self, tools: AutomationConfigTools
    ) -> None:
        """Core refused the write; the sections behind the best-practice
        warning come inline, so the fix needs no second call, and the whole
        canonical files do not."""
        require_vendored_skills()
        rejected = {**FLAGGED, "actions": [{"action": "not a service"}]}
        cited = check_automation_config(rejected).referenced_files

        body = await _refused(tools, config=rejected)

        assert set(body["skill_content"]) == cited
        assert all(body["skill_content"].values())
        assert next(iter(body)) == "skill_content_hint"
        assert _generic_hints(body["error"]["suggestions"])

    async def test_a_write_that_got_no_answer_carries_the_cited_sections_too(
        self, tools: AutomationConfigTools, core_client: HomeAssistantClient
    ) -> None:
        """A dropped connection is not a ToolError: the tool builds its error
        itself, and it carries the same guidance."""
        require_vendored_skills()
        cited = check_automation_config(FLAGGED).referenced_files

        with patch.object(
            core_client,
            "upsert_automation_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("dropped")),
        ):
            body = await _refused(tools, config=FLAGGED)

        assert body["error"]["code"] == "CONNECTION_FAILED"
        assert set(body["skill_content"]) == cited
        assert next(iter(body)) == "skill_content_hint"
        assert _generic_hints(body["error"]["suggestions"])
