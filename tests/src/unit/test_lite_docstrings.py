"""Unit tests for LiteDocstringsTransform and the server-side wiring.

Covers three layers:

1. ``LiteDocstringsTransform`` itself — empty-mapping no-op, mapped
   replacement, unmapped passthrough, on both ``list_tools`` and
   ``get_tool`` paths.
2. ``HomeAssistantSmartMCPServer._apply_lite_docstrings`` — the gate,
   the WARNING log, the import-error fallback, and the
   ``add_transform`` failure path. Uses the ``MagicMock`` stub pattern
   from ``test_categorized_search.TestApplySearchKeywordEnrichment``.
3. The ``_LITE_DOCSTRINGS`` mapping invariant — every lite description
   names the anchor that reaches its own declared destination, so the LLM
   still has a path to detailed guidance from inside the trimmed text.
4. The mapping's *ends* — that every key is a registered tool name, and
   that the guidance each entry defers to actually exists. Layer 3 checks
   the pointer text; layer 4 checks the two things that can be wrong
   without the text being malformed (#2153 review §2/§5).
5. ``BACKUP_HINT`` interpolation and the hand-copied prose tool lists in
   ``docs/beta.md`` / ``en.json`` (#2153 review §3/§4/§6).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ha_mcp.transforms import LiteDocstringsTransform
from ha_mcp.utils import skill_loader

REPO_ROOT = Path(__file__).parent.parent.parent.parent

# The generator is a script, not a package module — same import route
# test_tool_docs_sync and the locale-parity checks use.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import extract_tools  # noqa: E402

_SKILL_NAME = "home-assistant-best-practices"
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")
_TOOL_RESPONSE_PREFIX = "tool-response:"
_SELF_CONTAINED = "self-contained"
_REFERENCE_FILE_RE = re.compile(r"references/[\w.-]+\.md")


def _require_vendored_skills() -> Path:
    """Skip cleanly when the submodule isn't initialised.

    Mirrors ``test_skill_content_wiring._require_vendored_skills``.
    """
    skills_dir = skill_loader.get_skills_dir()
    if skills_dir is None or not (skills_dir / _SKILL_NAME).is_dir():
        pytest.skip(
            "skills-vendor submodule not initialised; "
            "run `git submodule update --init` to enable real-skill tests"
        )
    return skills_dir


def _make_tool(name: str, *, description: str = "") -> Tool:
    """Create a minimal Tool for testing."""

    async def noop() -> str:
        return "ok"

    return Tool.from_function(
        fn=noop,
        name=name,
        description=description,
        annotations=ToolAnnotations(readOnlyHint=True),
    )


_FULL_AUTOMATION = (
    "Get Home Assistant automation configuration.\n\n"
    "Returns the complete configuration including triggers, conditions, "
    "actions, and mode settings. (... many more paragraphs ...)"
)
_LITE_AUTOMATION = "Get a Home Assistant automation. See ha_get_skill_guide for schema."


@pytest.fixture
def replacements() -> dict[str, str]:
    return {"ha_config_get_automation": _LITE_AUTOMATION}


@pytest.fixture
def tools() -> Sequence[Tool]:
    return [
        _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION),
        _make_tool("ha_get_state", description="Get a state."),
    ]


# ---------------------------------------------------------------------------
# Layer 1: the transform itself
# ---------------------------------------------------------------------------


class TestListTools:
    @pytest.mark.asyncio
    async def test_empty_mapping_passes_through(self, tools: Sequence[Tool]) -> None:
        transform = LiteDocstringsTransform(replacements={})

        result = list(await transform.list_tools(tools))

        assert [t.description for t in result] == [
            _FULL_AUTOMATION,
            "Get a state.",
        ]

    @pytest.mark.asyncio
    async def test_none_mapping_passes_through(self, tools: Sequence[Tool]) -> None:
        """``None`` replacements coerces to ``{}`` — same as the empty case."""
        transform = LiteDocstringsTransform(replacements=None)

        result = list(await transform.list_tools(tools))

        assert [t.description for t in result] == [
            _FULL_AUTOMATION,
            "Get a state.",
        ]

    @pytest.mark.asyncio
    async def test_replaces_mapped_tools_only(
        self, tools: Sequence[Tool], replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(replacements=replacements)

        result = list(await transform.list_tools(tools))

        descriptions = {t.name: t.description for t in result}
        assert descriptions["ha_config_get_automation"] == _LITE_AUTOMATION
        assert descriptions["ha_get_state"] == "Get a state."


class TestGetTool:
    @pytest.mark.asyncio
    async def test_get_tool_empty_mapping_passes_through(self) -> None:
        transform = LiteDocstringsTransform(replacements={})
        original = _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION)
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_config_get_automation", call_next)

        assert result is not None
        assert result.description == _FULL_AUTOMATION

    @pytest.mark.asyncio
    async def test_get_tool_replaces_mapped(self, replacements: dict[str, str]) -> None:
        transform = LiteDocstringsTransform(replacements=replacements)
        original = _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION)
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_config_get_automation", call_next)

        assert result is not None
        assert result.description == _LITE_AUTOMATION

    @pytest.mark.asyncio
    async def test_get_tool_unmapped_passes_through(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(replacements=replacements)
        original = _make_tool("ha_get_state", description="Get a state.")
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_get_state", call_next)

        assert result is not None
        assert result.description == "Get a state."

    @pytest.mark.asyncio
    async def test_get_tool_missing_returns_none(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(replacements=replacements)
        call_next = AsyncMock(return_value=None)

        result = await transform.get_tool("ha_does_not_exist", call_next)

        assert result is None


# ---------------------------------------------------------------------------
# Layer 2: server.py wiring — mirrors TestApplySearchKeywordEnrichment
# ---------------------------------------------------------------------------


class TestApplyLiteDocstrings:
    """Tests for the server-side ``_apply_lite_docstrings`` wiring."""

    def _make_server_stub(self, *, enable_lite_docstrings: bool) -> MagicMock:
        """Minimal stub exposing only the attributes the method touches."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = MagicMock()
        stub._LITE_DOCSTRINGS = HomeAssistantSmartMCPServer._LITE_DOCSTRINGS
        # Bound classmethod — the real resolver, so the stub exercises
        # placeholder interpolation instead of handing back a MagicMock.
        stub._resolve_lite_docstrings = (
            HomeAssistantSmartMCPServer._resolve_lite_docstrings
        )
        stub.settings = MagicMock(enable_lite_docstrings=enable_lite_docstrings)
        stub.mcp = MagicMock()
        return stub

    def test_noop_when_flag_disabled(self) -> None:
        """When the flag is off, no transform is installed and no log."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=False)
        HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        stub.mcp.add_transform.assert_not_called()

    def test_installs_transform_when_flag_enabled(self) -> None:
        """When on, install a LiteDocstringsTransform with the real mapping.

        Compares keys and not identity: the installed mapping is the
        *resolved* one (``{backup_hint_text}`` filled in), so it is a fresh
        dict rather than ``_LITE_DOCSTRINGS`` itself.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=True)
        HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        stub.mcp.add_transform.assert_called_once()
        installed = stub.mcp.add_transform.call_args.args[0]
        assert isinstance(installed, LiteDocstringsTransform)
        assert set(installed._replacements) == set(
            HomeAssistantSmartMCPServer._LITE_DOCSTRINGS
        )

    def test_installed_descriptions_carry_no_unresolved_placeholders(self) -> None:
        """The catalog must never advertise a literal ``{token}``.

        The failure this guards is user-visible: a renamed or typo'd token
        leaves ``{backup_hint_text}`` sitting in the tool description an
        LLM reads.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=True)
        HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        installed = stub.mcp.add_transform.call_args.args[0]
        offenders = [
            name
            for name, lite in installed._replacements.items()
            if _PLACEHOLDER_RE.search(lite)
        ]
        assert not offenders, f"Unresolved placeholders in: {offenders}"

    def test_logs_warning_when_enabled(self, caplog) -> None:
        """The trade-off WARNING must be emitted so env-var users see it."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=True)
        with caplog.at_level("WARNING"):
            HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        assert any(
            "ENABLE_LITE_DOCSTRINGS=true" in rec.message
            and "may degrade LLM performance" in rec.message
            for rec in caplog.records
        )

    def test_transform_failure_logs_second_warning(self, caplog) -> None:
        """If add_transform fails, the user must know full descs remain."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_lite_docstrings=True)
        stub.mcp.add_transform.side_effect = RuntimeError("boom")

        with caplog.at_level("WARNING"):
            HomeAssistantSmartMCPServer._apply_lite_docstrings(stub)

        assert any(
            "failed to install" in rec.message
            and "full tool descriptions remain in effect" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Layer 3: the mapping invariant
# ---------------------------------------------------------------------------


class TestLiteDocstringsMappingInvariants:
    """Guard-rails on the user-visible lite descriptions themselves."""

    def test_every_lite_description_names_its_own_destination(self) -> None:
        """The design promise: every lite description points somewhere real.

        Without an anchor, the user-facing behaviour of the toggle regresses
        to "shorter descriptions, no guidance" the moment someone trims an
        entry too aggressively.

        Derived from ``_LITE_DOCSTRING_DESTINATIONS`` rather than hunting a
        fixed string. The previous version of this test required the literal
        ``"ha_get_skill_guide"`` in every value, which made it impossible to
        add a tool the skill pack doesn't cover WITHOUT manufacturing a
        dead-end pointer — and, worse, was satisfiable by a sentence saying
        the guide does *not* cover the tool. Now the required anchor follows
        from where the entry actually defers:

        * skill-path destination → must name ``ha_get_skill_guide``, the
          tool that serves it.
        * ``tool-response:<field>`` → must name ``<field>``. It may still
          mention ``ha_get_skill_guide``; ``ha_report_issue`` does, to say
          the guide does NOT cover issue reporting, which saves a compliant
          agent a pointless call. Only the field name is required here.
        * ``self-contained`` → must carry NO pointer at all. An entry that
          defers nothing cannot be allowed to quietly re-acquire a
          ``ha_get_skill_guide`` pointer or name a reference file, because
          that is how a description starts advertising content the pinned
          submodule does not contain. No entry declares this today — the
          arm is kept so a tool the skill pack does not cover has an
          honest option other than a dead pointer.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        offenders: list[str] = []
        for name, lite in HomeAssistantSmartMCPServer._LITE_DOCSTRINGS.items():
            dest = HomeAssistantSmartMCPServer._LITE_DOCSTRING_DESTINATIONS[name]
            if dest == _SELF_CONTAINED:
                if "ha_get_skill_guide" in lite or _REFERENCE_FILE_RE.search(lite):
                    offenders.append(
                        f"{name}: declared self-contained but the description "
                        "still points somewhere — either vendor the "
                        "destination and declare it, or drop the pointer"
                    )
            elif dest.startswith(_TOOL_RESPONSE_PREFIX):
                field = dest[len(_TOOL_RESPONSE_PREFIX) :]
                if f"`{field}`" not in lite:
                    offenders.append(
                        f"{name}: defers to its own {field!r} response field "
                        "but the description never names it"
                    )
            elif "ha_get_skill_guide" not in lite:
                offenders.append(
                    f"{name}: defers to {dest!r} but the description has no "
                    "ha_get_skill_guide pointer to reach it"
                )

        assert not offenders, (
            "Lite descriptions with an unreachable anchor:\n"
            + "\n".join(f"  - {entry}" for entry in offenders)
        )

    def test_every_lite_description_starts_with_action_verb(self) -> None:
        """AGENTS.md tool-docstring rule: first word is an action verb.

        Verb list mirrors AGENTS.md > Tool Docstrings > Required for
        every tool. ``Create`` covers ``Create or update`` openers used
        on the consolidated set_* tools.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        accepted = {
            "Get",
            "List",
            "Search",
            "Create",
            "Update",
            "Delete",
            "Remove",
            "Execute",
            "Call",
            "Manage",
        }
        # Strip trailing punctuation so multi-action openers (e.g.,
        # "Update, replace, or remove ...") still validate against the
        # leading verb.
        punctuation = ",.;:"
        offenders: list[tuple[str, str]] = []
        for name, lite in HomeAssistantSmartMCPServer._LITE_DOCSTRINGS.items():
            first_word = lite.split(maxsplit=1)[0].rstrip(punctuation)
            if first_word not in accepted:
                offenders.append((name, first_word))

        assert not offenders, (
            f"Lite descriptions not starting with an action verb: {offenders}"
        )


# ---------------------------------------------------------------------------
# Layer 4: the mapping's keys and its deferral destinations must be real
#
# The three tests above check the lite TEXT. They cannot catch either of the
# two ways an entry is wrong without being malformed: a key that matches no
# registered tool (silently no-ops in LiteDocstringsTransform._rewrite), or
# a pointer to guide content that does not exist.
# ---------------------------------------------------------------------------


class TestLiteDocstringsKeysAndDestinations:
    def test_every_key_resolves_to_a_registered_tool(self) -> None:
        """A key that matches no tool is a silent no-op.

        ``LiteDocstringsTransform._rewrite`` does ``.get(tool.name)`` and
        returns the tool untouched on a miss — no log, no error. So a
        typo'd or renamed key means that tool quietly keeps its full
        description while the toggle reports success. Nothing else checks
        this: ``TestApplyLiteDocstrings`` stubs ``mcp`` with a MagicMock,
        so real tool names never enter those tests at any layer.

        Resolves against the AST-extracted catalog rather than a live
        server, so it needs no HA instance.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        real = {t["name"] for t in extract_tools.extract_tools()}
        unknown = sorted(set(HomeAssistantSmartMCPServer._LITE_DOCSTRINGS) - real)

        assert not unknown, (
            "_LITE_DOCSTRINGS keys matching no registered tool (these "
            f"entries silently do nothing): {unknown}"
        )

    def test_every_mapped_tool_declares_a_destination(self) -> None:
        """Both maps must cover exactly the same tools.

        Keeps ``test_every_lite_destination_resolves`` honest — without
        this, adding an entry to ``_LITE_DOCSTRINGS`` and forgetting the
        destination would skip the resolution check rather than fail it.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        lite = set(HomeAssistantSmartMCPServer._LITE_DOCSTRINGS)
        declared = set(HomeAssistantSmartMCPServer._LITE_DOCSTRING_DESTINATIONS)

        assert lite == declared, (
            "_LITE_DOCSTRINGS and _LITE_DOCSTRING_DESTINATIONS disagree.\n"
            f"  missing a destination: {sorted(lite - declared)}\n"
            f"  destination with no lite entry: {sorted(declared - lite)}"
        )

    def test_every_lite_destination_resolves(self) -> None:
        """The design promise, checked at the destination end.

        ``test_every_lite_description_names_its_own_destination`` asserts
        the pointer STRING is present; it cannot tell whether the guide holds
        anything on the subject. An entry deferring to content that does
        not exist is worse than the documented trade-off ("the LLM might
        skip the extra call"), because a compliant agent that does follow
        the pointer also comes back with nothing.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        skills_dir = _require_vendored_skills()
        failures: list[str] = []

        for (
            tool,
            dest,
        ) in HomeAssistantSmartMCPServer._LITE_DOCSTRING_DESTINATIONS.items():
            if dest.startswith(_TOOL_RESPONSE_PREFIX):
                field = dest[len(_TOOL_RESPONSE_PREFIX) :]
                if not _tool_response_has_field(tool, field):
                    failures.append(
                        f"{tool} defers to its own response field "
                        f"{field!r}, which its source does not return"
                    )
                continue

            if dest == _SELF_CONTAINED:
                # Nothing to resolve: the entry advertises no destination.
                # test_every_lite_description_names_its_own_destination is
                # what holds it to that.
                continue

            resolved = skill_loader.resolve_skill_files(skills_dir, _SKILL_NAME, [dest])
            if not resolved.get(dest):
                failures.append(
                    f"{tool} defers to {dest!r}, which does not exist in "
                    f"the bundled {_SKILL_NAME} skill"
                )

        assert not failures, (
            "Lite deferral destinations that do not resolve:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )

    def test_named_reference_files_match_the_destination_map(self) -> None:
        """A lite text that names a ``references/*.md`` file must name its own.

        Two places can state the destination — the prose an LLM reads and
        the map the tests read. If they disagree, one of them is lying to
        somebody.

        Live as of the ``references/backups.md`` pin bump:
        ``ha_manage_backup``'s lite text cites that file inline and its
        destination names the same one, so a future edit that changes one
        without the other fails here.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        pattern = re.compile(r"references/[\w.-]+\.md")
        mismatches: list[str] = []
        for tool, lite in HomeAssistantSmartMCPServer._LITE_DOCSTRINGS.items():
            named = set(pattern.findall(lite))
            if not named:
                continue
            dest = HomeAssistantSmartMCPServer._LITE_DOCSTRING_DESTINATIONS[tool]
            if named != {dest}:
                mismatches.append(
                    f"{tool}: text names {sorted(named)}, map says {dest!r}"
                )

        assert not mismatches, "\n".join(mismatches)


class TestBackupHintInterpolation:
    """``BACKUP_HINT`` must survive the lite swap (#2153 review §3).

    ``_LITE_DOCSTRINGS`` is a static ``ClassVar`` while the full
    ``ha_manage_backup`` description is an f-string built per registration.
    Pinning one wording would leave a user setting Backup-hint to Strong,
    the UI confirming it, and nothing changing.
    """

    @pytest.mark.parametrize("level", ["strong", "normal", "weak", "auto"])
    def test_configured_hint_text_reaches_the_lite_description(
        self, level: str, monkeypatch
    ) -> None:
        from ha_mcp.server import HomeAssistantSmartMCPServer
        from ha_mcp.tools.backup import _get_backup_hint_text

        monkeypatch.setenv("BACKUP_HINT", level)
        expected = _get_backup_hint_text()

        resolved = HomeAssistantSmartMCPServer._resolve_lite_docstrings()

        assert expected in resolved["ha_manage_backup"]

    def test_hint_levels_produce_different_lite_text(self, monkeypatch) -> None:
        """Guards the no-op failure directly, not just the mechanism."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        monkeypatch.setenv("BACKUP_HINT", "strong")
        strong = HomeAssistantSmartMCPServer._resolve_lite_docstrings()[
            "ha_manage_backup"
        ]
        monkeypatch.setenv("BACKUP_HINT", "weak")
        weak = HomeAssistantSmartMCPServer._resolve_lite_docstrings()[
            "ha_manage_backup"
        ]

        assert strong != weak

    def test_every_placeholder_in_the_map_is_resolvable(self) -> None:
        """A typo'd token would otherwise ship verbatim in the catalog."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        known = set(HomeAssistantSmartMCPServer._lite_docstring_tokens())
        unknown: list[str] = []
        for name, lite in HomeAssistantSmartMCPServer._LITE_DOCSTRINGS.items():
            unknown.extend(
                f"{name}: {{{token}}}"
                for token in (m.group(0)[1:-1] for m in _PLACEHOLDER_RE.finditer(lite))
                if token not in known
            )

        assert not unknown, f"Placeholders with no resolver: {unknown}"


class TestBackupLiteDescriptionSize:
    """The claim the comments used to make as a number, made as a test.

    An exact "4571 -> 1295, 72%" pair went stale across three successive
    edits of the lite text, twice unnoticed. A ratio measured at test time
    cannot: it fails if someone re-inflates the lite value toward the full
    one, and it does not need touching when the wording changes.

    ``ha_manage_backup``'s advertised text is the ``description=`` f-string
    built in ``register_backup_tools()``, not a docstring (``__doc__`` is 73
    chars), so the full side is measured by extracting that template and
    substituting the hint the same way registration does.
    """

    def _full_description(self, hint_level: str, monkeypatch) -> str:
        monkeypatch.setenv("BACKUP_HINT", hint_level)
        from ha_mcp.tools.backup import _get_backup_hint_text

        source = (REPO_ROOT / "src" / "ha_mcp" / "tools" / "backup.py").read_text(
            encoding="utf-8"
        )
        opener = 'manage_backup_description = f"""'
        start = source.index(opener) + len(opener)
        template = source[start : source.index('"""', start)]
        return template.replace("{backup_hint_text}", _get_backup_hint_text())

    @pytest.mark.parametrize("hint_level", ["strong", "normal", "weak"])
    def test_backup_lite_description_stays_substantially_smaller(
        self, hint_level: str, monkeypatch
    ) -> None:
        from ha_mcp.server import HomeAssistantSmartMCPServer

        full = self._full_description(hint_level, monkeypatch)
        lite = HomeAssistantSmartMCPServer._resolve_lite_docstrings()[
            "ha_manage_backup"
        ]
        assert len(lite) < 0.45 * len(full), (
            f"[BACKUP_HINT={hint_level}] lite is {len(lite)} chars against a "
            f"full {len(full)} ({100 * len(lite) / len(full):.0f}%) — the "
            "entry is meant to be a fraction of the full description, and "
            "the whole point of mapping it was catalog size"
        )
        # And it must not be trimmed back to uselessness: the safety content
        # is why this entry accepts ~70% instead of ~82%.
        assert len(lite) > 900, (
            f"[BACKUP_HINT={hint_level}] lite is only {len(lite)} chars — "
            "check the irreversibility markers below are still inline"
        )

    @pytest.mark.parametrize(
        "marker",
        [
            "confirm=True",
            "enable_snapshot_delete",
            "snapshot_delete_min_age_days",
            "restarts HA",
            "enable_auto_backup",
            "safety snapshot",
            "heartbeats",
        ],
    )
    def test_backup_lite_description_keeps_its_safety_content(
        self, marker: str
    ) -> None:
        """Every irreversibility marker and no-fallback diagnostic, pinned.

        ``destructiveHint`` is set on this tool and the stated reason for
        accepting a smaller reduction is that these stay inline — but every
        other guard in this file passed with these sentences deleted, which
        is exactly the regression the review caught twice.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        lite = HomeAssistantSmartMCPServer._resolve_lite_docstrings()[
            "ha_manage_backup"
        ]
        assert marker in lite, (
            f"{marker!r} dropped from the ha_manage_backup lite description; "
            "it has no fallback in the skill pack or the input schema"
        )


class TestDocumentedToolLists:
    """The mapped-tool list is hand-copied into docs and the settings UI."""

    def test_documented_tool_lists_cover_every_mapped_tool(self) -> None:
        """Every mapped tool must be named in each documented list.

        Three regions, checked independently rather than as one blob:
        ``beta.md``'s beta-tools row, ``beta.md``'s
        ``### enable_lite_docstrings`` summary sentence, and the ``en.json``
        help string.

        The previous version matched over the WHOLE of ``beta.md``, which
        made it ineffective: ``ha_config_get_dashboard`` and
        ``ha_config_set_dashboard`` also appear in the dashboard-screenshot
        row, so both lite lists could be stripped of them and this still
        passed — verified by mutation. A companion test sentinelled one tool
        at count >= 2, which a 16th tool added to only one passage also
        slipped past. ``beta.md`` meanwhile *claimed* the guarantee neither
        delivered, and this PR exists partly because ``ha_search`` drifted
        out of all three lists unnoticed.

        Names are matched in their backticked form in Markdown and on word
        boundaries in JSON, so ``ha_search`` is not satisfied by
        ``ha_search_tools``.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        mapped = sorted(HomeAssistantSmartMCPServer._LITE_DOCSTRINGS)
        beta = (REPO_ROOT / "docs" / "beta.md").read_text(encoding="utf-8")

        # Region 1: the beta-tools table row (the env-var string is unique to
        # it). Region 2: the summary sentence under the section heading.
        rows = [ln for ln in beta.splitlines() if "ENABLE_LITE_DOCSTRINGS=true" in ln]
        assert len(rows) == 1, (
            f"expected exactly one beta-tools row for the flag, found {len(rows)}"
        )
        summaries = [
            ln
            for ln in beta.splitlines()
            if ln.startswith("Replaces the docstrings on")
        ]
        assert len(summaries) == 1, (
            "expected exactly one 'Replaces the docstrings on ...' summary "
            f"sentence in docs/beta.md, found {len(summaries)}"
        )

        help_text = json.loads(
            (
                REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales" / "en.json"
            ).read_text(encoding="utf-8")
        )["messages"]["features.enable_lite_docstrings.help"]

        regions = {
            "docs/beta.md (beta-tools row)": ("md", rows[0]),
            "docs/beta.md (enable_lite_docstrings summary)": ("md", summaries[0]),
            "en.json (features.enable_lite_docstrings.help)": ("json", help_text),
        }

        failures: list[str] = []
        for label, (kind, text) in regions.items():
            if kind == "md":
                missing = [n for n in mapped if f"`{n}`" not in text]
            else:
                missing = [
                    n for n in mapped if not re.search(rf"\b{re.escape(n)}\b", text)
                ]
            if missing:
                failures.append(f"{label} does not name: {missing}")

        assert not failures, (
            "Documented lite-docstring tool lists are out of sync with "
            "_LITE_DOCSTRINGS:\n" + "\n".join(f"  - {f}" for f in failures)
        )


def _tool_response_has_field(tool_name: str, field: str) -> bool:
    """True when ``tool_name``'s own function builds a dict carrying ``field``.

    AST check, scoped to the function whose name matches the tool inside the
    module ``extract_tools`` attributes it to. The previous version substring-
    matched ``'"<field>":'`` over the WHOLE source file, so any dict literal
    anywhere in the module satisfied it — including one never returned, which
    is the checked-the-pointer-never-the-destination pattern this test file
    exists to replace. Scoping to the function is still static (no live HA)
    and still an approximation — it accepts a dict built but not returned —
    but the dict must at least live in the tool's own body.

    Assumes the tool's function shares its registered name (true for every
    tool today); a mismatch returns False, which fails the destination test
    loudly rather than passing it silently.
    """
    sources = {
        t["source_file"]
        for t in extract_tools.extract_tools()
        if t["name"] == tool_name
    }
    tools_dir = REPO_ROOT / "src" / "ha_mcp" / "tools"

    def _dict_with_field_in_scope(node: ast.AST) -> bool:
        """Scan one function's own scope, not code nested inside it.

        A helper or lambda defined inside the tool has its own return
        value, so a dict it builds is not the tool's response — descending
        into it would re-open the any-dict-anywhere hole one level down.
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef,
            ):
                continue
            if isinstance(child, ast.Dict) and any(
                isinstance(key, ast.Constant) and key.value == field
                for key in child.keys
            ):
                return True
            if _dict_with_field_in_scope(child):
                return True
        return False

    for source in sources:
        tree = ast.parse((tools_dir / source).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # ast.walk, not module-level lookup: registered tools are
            # methods on registrar classes (ha_report_issue lives inside
            # one), so the function has to be findable at any depth — only
            # the scan of its BODY stops at nested scope boundaries.
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == tool_name
            ):
                if _dict_with_field_in_scope(node):
                    return True
    return False
