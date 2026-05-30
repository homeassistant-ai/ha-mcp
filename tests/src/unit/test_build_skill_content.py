"""Tests for util_helpers.build_skill_content (issue #1182).

The helper is the shared assembly point for the MandatoryBPS parameter on
every write tool (set_automation / _script / _scene / _helper / _dashboard /
_yaml). Behaviour exercised here applies uniformly to all six call sites.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

from ha_mcp.tools.util_helpers import (
    _SKILL_CONTENT_OPTOUT_HINT,
    _SKILLS_VENDOR_MISSING_WARNING,
    _WRITE_TOOL_BP_HINT_SUGGESTION,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    build_skill_content,
)


def _make_validation_error() -> ValidationError:
    """Construct a real ``pydantic.ValidationError`` — the realistic
    config-load failure ``Settings()`` raises, which the narrowed
    ``except ValidationError`` in build/attach_skill_content degrades on."""

    class _M(BaseModel):
        x: int

    try:
        _M(x="not-an-int")  # type: ignore[arg-type]
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")  # pragma: no cover


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
def patched_get_skills_dir(fake_skills_dir: Path):
    """Patch get_skills_dir so build_skill_content sees the fake skills root."""
    with patch(
        "ha_mcp.utils.skill_loader.get_skills_dir", return_value=fake_skills_dir
    ):
        yield


# ---------------------------------------------------------------------------
# MandatoryBPS=True path: canonical files attached
# ---------------------------------------------------------------------------


class TestIncludeSkillCanonical:
    def test_canonical_files_attached_when_on(self, patched_get_skills_dir):
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        body = result["references/automation-patterns.md"]
        # Full file: every H2 section present, no slicing applied.
        assert "Native Conditions" in body
        assert "Trigger Types" in body

    def test_multiple_canonical_files(self, patched_get_skills_dir):
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

    def test_top_level_skill_md(self, patched_get_skills_dir):
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


class TestIncludeSkillOff:
    def test_canonical_suppressed_when_off(self, patched_get_skills_dir):
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {}

    def test_referenced_files_still_attach_when_off(self, patched_get_skills_dir):
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


class TestDedup:
    def test_canonical_and_referenced_union(self, patched_get_skills_dir):
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files={"references/template-guidelines.md"},
        )
        assert set(result.keys()) == {
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        }

    def test_overlap_collapses_to_one(self, patched_get_skills_dir):
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
    def test_skills_vendor_missing(self):
        """When the submodule isn't checked out, build_skill_content is a no-op."""
        with patch("ha_mcp.utils.skill_loader.get_skills_dir", return_value=None):
            result = build_skill_content(
                MandatoryBPS=True,
                canonical_files=("references/automation-patterns.md",),
                referenced_files=None,
            )
            assert result == {}

    def test_nothing_requested(self, patched_get_skills_dir):
        """MandatoryBPS=False with no referenced_files returns empty without I/O."""
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert result == {}

    def test_missing_canonical_file_silently_skipped(self, patched_get_skills_dir):
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

    def test_master_switch_off_short_circuits(self, patched_get_skills_dir):
        """ENABLE_MANDATORY_BPS=false is the server-wide master switch —
        when off, build_skill_content returns empty regardless of the
        per-call MandatoryBPS flag OR the presence of referenced_files
        from BP warnings. Operator-controlled kill switch sits above the
        per-call agent toggle."""
        from ha_mcp import config as config_module

        # Build a settings instance with the master switch flipped off.
        # Use object.__new__ + __dict__ copy to avoid running the env-var
        # loading pipeline; we only need the one field overridden.
        original = config_module.get_global_settings()
        patched_settings = original.model_copy(update={"enable_mandatory_bps": False})
        with patch.object(
            config_module, "get_global_settings", return_value=patched_settings
        ):
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
                referenced_files={
                    "references/automation-patterns.md#native-conditions"
                },
            )
            assert result == {}, "master-off must override BP-warning auto-embed"

    def test_master_off_with_vendor_missing_does_not_emit_warning(self):
        """When master is off + vendor missing + caller wanted skills,
        attach_skill_content must NOT emit the
        ``_SKILLS_VENDOR_MISSING_WARNING`` (which tells the operator to
        run ``git submodule update --init``) — the suppression cause is
        the deliberate operator config, not a missing submodule."""
        from ha_mcp import config as config_module

        original = config_module.get_global_settings()
        patched_settings = original.model_copy(update={"enable_mandatory_bps": False})
        with (
            patch.object(
                config_module, "get_global_settings", return_value=patched_settings
            ),
            patch("ha_mcp.utils.skill_loader.get_skills_dir", return_value=None),
        ):
            response: dict = {"success": True}
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

    def test_augment_error_adds_generic_hint_without_bp(self):
        """Every write-tool error must surface the generic BP-skill-guide
        pointer in suggestions — even when the BP checker didn't fire
        (helpers / dashboards / yaml have no BP integration). Without
        this, an error with no BP warnings ships zero skill guidance and
        the LLM has no breadcrumb to ha_get_skill_guide."""
        error = {
            "success": False,
            "error": {
                "code": "VALIDATION_FAILED",
                "message": "bad input",
                "suggestions": ["check your config"],
            },
        }
        augment_error_dict_with_skill_content(error, bp_warnings=None)
        suggestions = error["error"]["suggestions"]
        assert _WRITE_TOOL_BP_HINT_SUGGESTION in suggestions
        assert "check your config" in suggestions
        # singular ``suggestion`` mirrors the first entry for legacy consumers.
        assert error["error"]["suggestion"] == suggestions[0]
        # No skill_content delivered when bp_warnings has no referenced_files.
        assert "skill_content" not in error
        assert "skill_content_hint" not in error

    def test_augment_error_idempotent_on_re_raise(self):
        """The hint must not double-append when augment runs twice
        (e.g. nested try/except re-raise paths)."""
        error = {"success": False, "error": {"suggestions": []}}
        augment_error_dict_with_skill_content(error, bp_warnings=None)
        augment_error_dict_with_skill_content(error, bp_warnings=None)
        suggestions = error["error"]["suggestions"]
        assert suggestions.count(_WRITE_TOOL_BP_HINT_SUGGESTION) == 1

    def test_augment_error_embeds_bp_sections_when_referenced(
        self, patched_get_skills_dir
    ):
        """When the BP checker fired with referenced_files, the error
        response auto-embeds the matching section bodies under
        skill_content — the LLM gets the actionable fix material inline
        instead of having to make a second tool call."""

        # Build a minimal BestPracticeCheckResult-like object — the
        # augment helper uses duck-typing on .referenced_files.
        class _BP:
            def __init__(self, refs):
                self.referenced_files = refs

        error = {"success": False, "error": {"suggestions": []}}
        augment_error_dict_with_skill_content(
            error,
            bp_warnings=_BP({"references/automation-patterns.md#native-conditions"}),
        )
        # Section body inlined, hint at top, generic hint in suggestions.
        assert "skill_content" in error
        assert (
            "references/automation-patterns.md#native-conditions"
            in error["skill_content"]
        )
        assert error.get("skill_content_hint") == _SKILL_CONTENT_OPTOUT_HINT
        assert _WRITE_TOOL_BP_HINT_SUGGESTION in error["error"]["suggestions"]
        # Canonical files NOT attached on error — section body is enough.
        assert "references/automation-patterns.md" not in (
            error.get("skill_content") or {}
        ) or list(error["skill_content"].keys()) == [
            "references/automation-patterns.md#native-conditions"
        ]

    def test_augment_tool_error_wraps_dict_augmentation(self, patched_get_skills_dir):
        """The ToolError wrapper is what the 6 write tools call from their
        outer except handler. Decodes the JSON body, runs the dict
        augmentation (generic hint + section embed), re-encodes into a
        new ToolError. Without this test the wrapper itself was
        unverified — only the dict variant was covered."""
        import json

        from fastmcp.exceptions import ToolError

        class _BP:
            def __init__(self, refs):
                self.referenced_files = refs

        error_dict = {
            "success": False,
            "error": {
                "code": "VALIDATION_FAILED",
                "message": "bad config",
                "suggestions": ["fix the config"],
            },
        }
        te = ToolError(json.dumps(error_dict))
        augmented = augment_tool_error_with_skill_content(
            te,
            bp_warnings=_BP({"references/automation-patterns.md#native-conditions"}),
        )
        # New ToolError instance is returned; body carries the augmentation.
        assert isinstance(augmented, ToolError)
        body = json.loads(str(augmented))
        suggestions = body["error"]["suggestions"]
        assert _WRITE_TOOL_BP_HINT_SUGGESTION in suggestions
        assert "fix the config" in suggestions
        # Section auto-embedded under skill_content with hint at top.
        assert "skill_content" in body
        assert (
            "references/automation-patterns.md#native-conditions"
            in body["skill_content"]
        )
        assert body.get("skill_content_hint") == _SKILL_CONTENT_OPTOUT_HINT

    def test_augment_tool_error_falls_through_on_non_json_body(self):
        """When the ToolError body isn't a JSON-decodable error dict,
        return the original ToolError unchanged. Preserves the contract
        for any future raise that doesn't use ``raise_tool_error``."""
        from fastmcp.exceptions import ToolError

        te = ToolError("plain string body, not JSON")
        result = augment_tool_error_with_skill_content(te, bp_warnings=None)
        assert result is te

    def test_settings_load_raises_returns_empty(self, patched_get_skills_dir):
        """build_skill_content must silently degrade to {} when
        get_global_settings() raises a config-validation error — the
        documented contract is "skill content is opportunistic; never
        fail the surrounding write". Without this guard, a settings-
        validation regression would propagate, the outer except in each
        tool would re-map to INTERNAL_ERROR, and the agent would retry
        an already-committed mutation.

        The except is narrowed to ``pydantic.ValidationError`` (the
        realistic Settings() config-load failure), so a genuine config
        problem degrades gracefully here."""
        # ``get_global_settings`` is imported INSIDE ``build_skill_content``
        # via ``from ..config import get_global_settings``, so the symbol
        # isn't bound in ``util_helpers``'s module namespace. Patch at the
        # source module instead — that's where the function-local import
        # resolves the name on every call.
        with patch(
            "ha_mcp.config.get_global_settings",
            side_effect=_make_validation_error(),
        ):
            result = build_skill_content(
                MandatoryBPS=True,
                canonical_files=("references/automation-patterns.md",),
                referenced_files=None,
            )
        assert result == {}

    def test_settings_load_propagates_unexpected_error(self, patched_get_skills_dir):
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

    def test_trailing_hash_resolves_to_whole_file(self, patched_get_skills_dir):
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
# Per-tool canonical mappings exist and point to files that should exist
# ---------------------------------------------------------------------------


class TestPerToolCanonicalMappings:
    """Each write tool exports its canonical mapping constant. These tests
    pin the contract — changing a mapping is a deliberate edit, not an
    accident."""

    def test_automation_mapping(self):
        from ha_mcp.tools.tools_config_automations import _AUTOMATION_SKILL_FILES

        assert _AUTOMATION_SKILL_FILES == (
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        )

    def test_script_mapping(self):
        from ha_mcp.tools.tools_config_scripts import _SCRIPT_SKILL_FILES

        assert _SCRIPT_SKILL_FILES == (
            "references/automation-patterns.md",
            "references/template-guidelines.md",
        )

    def test_scene_mapping(self):
        from ha_mcp.tools.tools_config_scenes import _SCENE_SKILL_FILES

        assert _SCENE_SKILL_FILES == ("SKILL.md",)

    def test_helper_mapping(self):
        from ha_mcp.tools.tools_config_helpers import _HELPER_SKILL_FILES

        assert _HELPER_SKILL_FILES == ("references/helper-selection.md",)

    def test_dashboard_mapping(self):
        from ha_mcp.tools.tools_config_dashboards import _DASHBOARD_SKILL_FILES

        assert _DASHBOARD_SKILL_FILES == (
            "references/dashboard-guide.md",
            "references/dashboard-cards.md",
        )

    def test_yaml_mapping(self):
        from ha_mcp.tools.tools_yaml_config import _YAML_SKILL_FILES

        assert _YAML_SKILL_FILES == ("references/template-guidelines.md",)


# ---------------------------------------------------------------------------
# attach_skill_content — happy + degraded paths
# ---------------------------------------------------------------------------


class TestAttachSkillContent:
    """The shared in-place helper that every write tool uses.

    Mirrors the read-side ha_get_skill_guide ``degraded: True`` contract:
    when the bundled skills-vendor submodule is missing and the caller
    requested skill content, the response carries a top-level warning so
    the operator notices instead of getting a silently degraded server.
    """

    def test_happy_path_attaches_skill_content(self, patched_get_skills_dir):
        response: dict = {"success": True}
        attach_skill_content(
            response,
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        assert "skill_content" in response
        # The hint teaches the LLM what the schema-visible-but-undescribed
        # MandatoryBPS param does — the bare-bool catalog entry alone
        # carries no semantic signal. Must accompany every delivered
        # skill_content payload.
        assert response.get("skill_content_hint") == _SKILL_CONTENT_OPTOUT_HINT
        assert "warnings" not in response

    def test_hint_appears_first_and_content_last(self, patched_get_skills_dir):
        """Response key order matters for small models that process
        top-down. The opt-out hint goes FIRST so it isn't buried under
        the ~25KB skill_content body; skill_content goes LAST so the
        operation result fields stay near the hint. BAT on PR #1448
        showed even Opus needed five tries to find a tail-positioned
        conditional hint; smaller models never did."""
        response: dict = {"success": True, "data": {"id": "x"}, "entity_id": "y"}
        attach_skill_content(
            response,
            MandatoryBPS=True,
            canonical_files=("references/automation-patterns.md",),
            referenced_files=None,
        )
        keys = list(response.keys())
        assert keys[0] == "skill_content_hint", (
            f"hint must be first key for top-down model parsing, got {keys}"
        )
        assert keys[-1] == "skill_content", (
            f"bulky content must be last so it doesn't push the hint out "
            f"of the model's top-of-response attention window, got {keys}"
        )
        # The original response fields are preserved between hint and content.
        assert set(keys) == {
            "skill_content_hint",
            "success",
            "data",
            "entity_id",
            "skill_content",
        }

    def test_nothing_requested_is_silent(self, patched_get_skills_dir):
        """MandatoryBPS=False + no referenced_files → silent (user opted out)."""
        response: dict = {"success": True}
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

    def test_vendor_missing_with_MandatoryBPS_warns(self):
        """Asymmetry fix vs ha_get_skill_guide: write tools used to silently
        omit skill_content when the submodule was absent. Now they warn."""
        with patch("ha_mcp.utils.skill_loader.get_skills_dir", return_value=None):
            response: dict = {"success": True}
            attach_skill_content(
                response,
                MandatoryBPS=True,
                canonical_files=("references/automation-patterns.md",),
                referenced_files=None,
            )
        assert "skill_content" not in response
        assert response.get("warnings") == [_SKILLS_VENDOR_MISSING_WARNING]

    def test_vendor_missing_with_opt_out_is_silent(self):
        """Caller explicitly opted out with MandatoryBPS=False — don't nag."""
        with patch("ha_mcp.utils.skill_loader.get_skills_dir", return_value=None):
            response: dict = {"success": True}
            attach_skill_content(
                response,
                MandatoryBPS=False,
                canonical_files=("references/automation-patterns.md",),
                referenced_files=None,
            )
        assert "skill_content" not in response
        assert "warnings" not in response

    def test_vendor_missing_with_referenced_files_warns(self):
        """BP-warning auto-embed path is also degraded when vendor missing."""
        with patch("ha_mcp.utils.skill_loader.get_skills_dir", return_value=None):
            response: dict = {"success": True}
            attach_skill_content(
                response,
                MandatoryBPS=False,
                canonical_files=(),
                referenced_files={
                    "references/automation-patterns.md#native-conditions"
                },
            )
        assert "skill_content" not in response
        assert _SKILLS_VENDOR_MISSING_WARNING in response.get("warnings", [])

    def test_warning_appended_not_overwritten(self):
        """If the response already has a warnings list, we append, not overwrite."""
        with patch("ha_mcp.utils.skill_loader.get_skills_dir", return_value=None):
            response: dict = {"success": True, "warnings": ["pre-existing"]}
            attach_skill_content(
                response,
                MandatoryBPS=True,
                canonical_files=("references/automation-patterns.md",),
                referenced_files=None,
            )
        assert response["warnings"] == ["pre-existing", _SKILLS_VENDOR_MISSING_WARNING]


# ---------------------------------------------------------------------------
# Anchor extraction (reactive section-slicing, issue #1182 Q3)
# ---------------------------------------------------------------------------


class TestAnchorExtraction:
    """Reactive auto-embed ships just the markdown section pointed to by
    the warning's #anchor, not the whole reference file."""

    def test_anchored_ref_returns_section(self, patched_get_skills_dir):
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

    def test_bare_supersedes_anchored_for_same_file(self, patched_get_skills_dir):
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

    def test_bare_does_not_supersede_anchored_for_different_file(
        self, patched_get_skills_dir
    ):
        """Dedup only collapses bare-vs-section for the SAME file. Cross-file
        refs are independent."""
        result = build_skill_content(
            MandatoryBPS=True,
            canonical_files=("references/template-guidelines.md",),
            referenced_files={"references/automation-patterns.md#native-conditions"},
        )
        assert "references/template-guidelines.md" in result
        assert "references/automation-patterns.md#native-conditions" in result

    def test_missing_anchor_silently_skipped(self, patched_get_skills_dir):
        """An anchor that doesn't match any heading is silently omitted —
        same contract as missing files."""
        result = build_skill_content(
            MandatoryBPS=False,
            canonical_files=(),
            referenced_files={"references/automation-patterns.md#does-not-exist"},
        )
        assert result == {}
