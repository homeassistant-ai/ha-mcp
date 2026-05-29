"""E2E coverage for the write-tool skill_content delivery feature (#1182).

Three scenarios per the design spec:

* MandatoryBPS=True (default): canonical best-practice files attach
  under ``skill_content`` and ``skill_content_hint`` ships at the top
  of the response.
* MandatoryBPS=False (explicit opt-out): no ``skill_content``, no
  ``skill_content_hint`` — the LLM signalled it has the content.
* BP-warning auto-embed: an anti-pattern input that HA still accepts
  fires the reactive checker; with MandatoryBPS=False the response
  carries ONLY the matched section body (not the whole canonical file),
  proving section-slicing works end-to-end.

These tests run through real FastMCP + a real HA container, so they
exercise the wiring the AST-based unit tests in
``test_skill_content_wiring.py`` only pin structurally.
"""

import logging

import pytest

from ...utilities.assertions import parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)

_AUTOMATION_PATTERNS_REF = "references/automation-patterns.md"
_TEMPLATE_GUIDELINES_REF = "references/template-guidelines.md"
_NATIVE_CONDITIONS_ANCHOR = "references/automation-patterns.md#native-conditions"


@pytest.mark.automation
@pytest.mark.cleanup
class TestSkillContentDelivery:
    """End-to-end skill_content delivery on ha_config_set_automation."""

    async def _find_test_light_entity(self, mcp_client) -> str:
        """Find a light entity to use as the automation target."""
        search = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 5},
        )
        data = parse_mcp_result(search)
        results = data.get("data", {}).get("results") or data.get("results", [])
        assert results, "no light entities found in test HA instance"
        return results[0]["entity_id"]

    async def test_default_mandatorybps_attaches_canonical_skill_content(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """MandatoryBPS=True (the default) attaches the canonical reference
        files under skill_content with the opt-out hint at the top."""
        test_light = await self._find_test_light_entity(mcp_client)
        config = test_data_factory.automation_config(
            "Skill Content Default E2E",
            trigger=[{"platform": "time", "at": "07:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        # No MandatoryBPS passed → defaults to True.
        result = await safe_call_tool(
            mcp_client, "ha_config_set_automation", {"config": config}
        )
        assert result.get("success"), f"automation create failed: {result}"

        entity_id = result.get("entity_id")
        if entity_id:
            cleanup_tracker.track("automation", entity_id)

        # Hint at the top of the response so small models notice it
        # before parsing the ~25 KB content body. The FIRST key of the
        # final response must be skill_content_hint.
        keys = list(result.keys())
        assert keys[0] == "skill_content_hint", (
            f"skill_content_hint must be first key, got {keys}"
        )
        # Canonical files attached.
        skill_content = result.get("skill_content")
        assert skill_content, "skill_content must be non-empty"
        assert _AUTOMATION_PATTERNS_REF in skill_content
        assert _TEMPLATE_GUIDELINES_REF in skill_content

    async def test_mandatorybps_false_suppresses_skill_content(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """Explicit MandatoryBPS=False ships no skill_content / no hint —
        the LLM signalled it already has the references in context."""
        test_light = await self._find_test_light_entity(mcp_client)
        config = test_data_factory.automation_config(
            "Skill Content OptOut E2E",
            trigger=[{"platform": "time", "at": "07:01:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": config, "MandatoryBPS": False},
        )
        assert result.get("success"), f"automation create failed: {result}"

        entity_id = result.get("entity_id")
        if entity_id:
            cleanup_tracker.track("automation", entity_id)

        assert "skill_content" not in result, (
            "MandatoryBPS=False must suppress skill_content"
        )
        assert "skill_content_hint" not in result, (
            "MandatoryBPS=False must suppress skill_content_hint"
        )

    async def test_bp_warning_auto_embeds_only_relevant_section(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """When the BP checker fires on input AND MandatoryBPS=False (so
        canonical files are suppressed), the response carries ONLY the
        anchored section body cited by the BP warning — not the whole
        reference file. Pins the section-slicing path end-to-end.

        Template-in-condition with float-comparison is the canonical
        anti-pattern the checker catches; the warning anchors at
        ``automation-patterns.md#native-conditions``."""
        test_light = await self._find_test_light_entity(mcp_client)
        config = test_data_factory.automation_config(
            "Skill Content Section Slice E2E",
            trigger=[{"platform": "time", "at": "07:02:00"}],
            condition=[
                {
                    "condition": "template",
                    "value_template": "{{ states('sensor.fake_temp') | float > 25 }}",
                }
            ],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        # MandatoryBPS=False suppresses canonical attach; only the
        # BP-warning referenced section should embed.
        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": config, "MandatoryBPS": False},
        )
        assert result.get("success"), f"automation create failed: {result}"

        entity_id = result.get("entity_id")
        if entity_id:
            cleanup_tracker.track("automation", entity_id)

        # BP checker fired (warning text surfaced).
        bp_warnings = result.get("best_practice_warnings") or []
        assert bp_warnings, (
            "expected best_practice_warnings on template-in-condition input"
        )
        # The section-anchored ref is embedded — and ONLY that ref, not
        # the whole automation-patterns.md file. Section-slicing in
        # action.
        skill_content = result.get("skill_content") or {}
        assert skill_content, "expected section auto-embed on BP warning"
        assert _NATIVE_CONDITIONS_ANCHOR in skill_content, (
            f"expected {_NATIVE_CONDITIONS_ANCHOR} key, got "
            f"{list(skill_content.keys())}"
        )
        # The bare-file canonical must NOT be present (would mean
        # canonical attach leaked through despite MandatoryBPS=False).
        assert _AUTOMATION_PATTERNS_REF not in skill_content, (
            "bare canonical file should be suppressed by MandatoryBPS=False"
        )
        # Section body is small: it contains just the matching heading,
        # NOT the whole file's other top-level sections.
        section_body = skill_content[_NATIVE_CONDITIONS_ANCHOR]
        # The section body starts with the matching heading and is
        # bounded by the next same/higher-level heading.
        assert section_body.lstrip().startswith("##"), (
            "section body should start with a markdown heading"
        )

    # ------------------------------------------------------------------
    # Coverage across the other write tools (script / scene / helper /
    # dashboard). Automation is covered explicitly above; yaml — which is
    # feature-flagged and custom-component-gated — is covered in
    # tests/src/e2e/workflows/filesystem/test_yaml_config.py, which owns
    # the mcp_client_with_yaml_config fixture. Together these exercise all
    # six write tools that expose the MandatoryBPS parameter, each of
    # which has a distinct success-return shape (spread dicts, helper
    # subentry paths, dashboard metadata) where an ordering or wrong-dict
    # bug would slip past the structural AST test.
    # ------------------------------------------------------------------

    def _build_create(self, tool: str, light: str, suffix: str):
        """Return (tool_name, args, expected_skill_substrings) for a
        minimal valid create on each write tool."""
        if tool == "script":
            return (
                "ha_config_set_script",
                {
                    "script_id": f"e2e_skill_script_{suffix}",
                    "config": {
                        "alias": f"E2E Skill Script {suffix}",
                        "sequence": [
                            {
                                "service": "light.turn_on",
                                "target": {"entity_id": light},
                            }
                        ],
                    },
                },
                ["automation-patterns.md", "template-guidelines.md"],
            )
        if tool == "scene":
            return (
                "ha_config_set_scene",
                {
                    "scene_id": f"e2e_skill_scene_{suffix}",
                    "config": {
                        "name": f"E2E Skill Scene {suffix}",
                        "entities": {light: {"state": "on"}},
                    },
                },
                ["SKILL.md"],
            )
        if tool == "helper":
            return (
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    "name": f"E2E Skill Helper {suffix}",
                },
                ["helper-selection.md"],
            )
        if tool == "dashboard":
            return (
                "ha_config_set_dashboard",
                {
                    "url_path": f"e2e-skill-dashboard-{suffix}",
                    "title": f"E2E Skill Dash {suffix}",
                    "config": {"views": [{"title": "V", "cards": []}]},
                },
                ["dashboard-guide.md", "dashboard-cards.md"],
            )
        raise AssertionError(f"unknown tool {tool}")

    def _track(self, cleanup_tracker, tool: str, args: dict, result: dict) -> None:
        """Best-effort cleanup registration for the created entity."""
        entity_id = result.get("entity_id")
        if tool == "script":
            cleanup_tracker.track("script", entity_id or f"script.{args['script_id']}")
        elif tool == "scene":
            cleanup_tracker.track("scene", entity_id or f"scene.{args['scene_id']}")
        elif tool == "helper":
            if entity_id:
                cleanup_tracker.track("input_boolean", entity_id)
        elif tool == "dashboard":
            cleanup_tracker.track("dashboard", args["url_path"])

    @pytest.mark.parametrize("tool", ["script", "scene", "helper", "dashboard"])
    async def test_default_on_attaches_skill_content_all_tools(
        self, mcp_client, cleanup_tracker, tool
    ):
        """Default MandatoryBPS=True attaches canonical skill_content with
        the hint as the FIRST response key, for every write tool — not
        just automation."""
        light = await self._find_test_light_entity(mcp_client)
        tool_name, args, expected = self._build_create(tool, light, "on")
        result = await safe_call_tool(mcp_client, tool_name, args)
        assert result.get("success"), f"{tool} create failed: {result}"
        self._track(cleanup_tracker, tool, args, result)

        keys = list(result.keys())
        assert keys[0] == "skill_content_hint", (
            f"{tool}: skill_content_hint must be the first response key, got {keys}"
        )
        skill_content = result.get("skill_content") or {}
        assert skill_content, f"{tool}: skill_content must be non-empty"
        joined = "\n".join(skill_content.keys())
        for sub in expected:
            assert sub in joined, (
                f"{tool}: expected {sub!r} among skill_content keys "
                f"{list(skill_content.keys())}"
            )

    @pytest.mark.parametrize("tool", ["script", "scene", "helper", "dashboard"])
    async def test_mandatorybps_false_suppresses_all_tools(
        self, mcp_client, cleanup_tracker, tool
    ):
        """Explicit MandatoryBPS=False suppresses both skill_content and the
        hint, for every write tool."""
        light = await self._find_test_light_entity(mcp_client)
        tool_name, args, _ = self._build_create(tool, light, "off")
        result = await safe_call_tool(
            mcp_client, tool_name, {**args, "MandatoryBPS": False}
        )
        assert result.get("success"), f"{tool} create failed: {result}"
        self._track(cleanup_tracker, tool, args, result)

        assert "skill_content" not in result, (
            f"{tool}: MandatoryBPS=False must suppress skill_content"
        )
        assert "skill_content_hint" not in result, (
            f"{tool}: MandatoryBPS=False must suppress skill_content_hint"
        )
