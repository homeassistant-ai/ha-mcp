"""``.coderabbit.yaml`` must stay valid and match what AGENTS.md claims.

Nothing in CI parses this file — no yamllint, no schema check — and every key
it omits falls back to CodeRabbit's schema defaults rather than to any UI
setting. ``reviews.auto_review.drafts`` defaults to ``false``, so a typo'd key
or a bad indent silently restores the exact behaviour the file exists to
change, with no signal anywhere. The guideline paths have the same shape of
failure: rename ``.gemini/styleguide.md`` and both CodeRabbit and the Codex
review request point at nothing, quietly.

Same pattern as ``test_ruff_scope_covers_the_tree.py`` (pins a tool's config to
the tree) and ``test_locale_parity.py::test_locale_readme_states_the_current_ceilings``
(pins the canonical locale guide to the real values).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CODERABBIT_YAML = REPO_ROOT / ".coderabbit.yaml"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
CODEX_DELIVERY = REPO_ROOT / ".github" / "workflows" / "pr-codex-review-delivery.yml"
CODEX_REQUEST = REPO_ROOT / ".github" / "workflows" / "pr-codex-review-request.yml"
STYLEGUIDE = ".gemini/styleguide.md"
REPO_WIDE_GUIDELINES = {
    STYLEGUIDE,
    "docs/agents/*.md",
    "src/ha_mcp/settings_ui/locales/README.md",
}

# The exact auto-review skip list, mirrored by the Codex auto-admission list
# in pr-codex-review-request.yml. dependabot/renovate PRs have never been
# reviewed. github-actions[bot] authors the webhook-proxy promote PRs — dev ->
# stable copies whose content was already reviewed in the dev PRs — excluded
# knowingly (maintainer decision on #2118); a promote that folds in more than
# a version bump still gets `@coderabbitai review` on demand. Exact logins:
# CodeRabbit matches ``ignore_usernames`` case-sensitively with no wildcard
# support.
BOT_AUTHORS = {
    "dependabot[bot]",
    "ha-mcp-renovate[bot]",
    "github-actions[bot]",
}

# The issue_comment `/review` admission list is DELIBERATELY narrower than
# BOT_AUTHORS: github-actions[bot] must stay off it, or a maintainer loses the
# on-demand Codex lever on promote PRs — the escape hatch the auto-exclusion
# rationale rests on. Harmonising the two lists would read as tidying while
# quietly turning "excluded from automatic review" into "not reviewed at all".
COMMENT_PATH_SKIPS = {
    "dependabot[bot]",
    "ha-mcp-renovate[bot]",
}

# ``applyTo`` has to keep the styleguide repo-wide, and minimatch's ``dot:
# false`` means each class of path needs its own pattern. Asserted separately
# rather than as one exact string: dropping any single one silently removes a
# whole class of files from the styleguide's scope, and that is the failure
# worth naming. Adding further patterns is fine.
REQUIRED_SCOPE = {
    "**": "non-dot paths at any depth",
    "**/.*": "dotfiles at any depth, including the repo root",
    "**/.*/**": "contents of dot-directories (.github/, .claude/, .gemini/)",
}


def _config() -> dict[str, Any]:
    loaded = yaml.safe_load(CODERABBIT_YAML.read_text("utf-8"))
    assert isinstance(loaded, dict), f"{CODERABBIT_YAML.name} must parse to a mapping"
    return loaded


def _subsection(text: str, title: str) -> str | None:
    """The body of one ``### `` subsection, or ``None`` when absent.

    Scoped rather than grepping the whole document, for the reason given in
    ``test_locale_parity.py::_locale_readme_section``: a whole-file grep answers a
    question about the file, not about the section it claims to guard.

    Terminates on any heading of level 1-3 or end of input. Both bounds matter:
    stopping only at ``##``/``###`` would report a section that ends the file as
    missing, and would run a following ``#`` section's text into the body, so a
    value assertion could pass on prose from somewhere else entirely. ``####``
    and deeper stay inside the body, being parts of this section.
    """
    match = re.search(
        rf"^### {re.escape(title)}$(.*?)(?=^#{{1,3}} |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else None


def _agents_md_subsection(title: str) -> str:
    """``_subsection`` against AGENTS.md, asserting the section exists."""
    body = _subsection(AGENTS_MD.read_text("utf-8"), title)
    assert body is not None, (
        f"AGENTS.md has no '### {title}' section — this test guards it"
    )
    return body


def test_subsection_bounds_hold_at_every_edge() -> None:
    """The extractor decides what every prose assertion below is reading."""
    assert _subsection("### A\nbody\n### B\nother\n", "A") == "\nbody\n"
    assert _subsection("### A\nbody\n## B\nother\n", "A") == "\nbody\n"
    # A following h1 must terminate it — otherwise the next top-level section's
    # prose lands in the body and a value assertion can pass on the wrong text.
    assert _subsection("### A\nbody\n# B\nother\n", "A") == "\nbody\n"
    # Last section in the file: the ##/###-only bound reported this as missing.
    assert _subsection("### A\nbody\n", "A") == "\nbody\n"
    # Deeper headings belong to the section.
    assert _subsection("### A\nbody\n#### A1\nmore\n### B\n", "A") == (
        "\nbody\n#### A1\nmore\n"
    )
    assert _subsection("### A\nbody\n", "Missing") is None


def test_draft_reviews_are_enabled() -> None:
    """The whole point of the file; CodeRabbit's own default is ``false``."""
    auto_review = _config()["reviews"]["auto_review"]

    assert auto_review["drafts"] is True, (
        "reviews.auto_review.drafts must be true — every PR here opens as a "
        "draft (AGENTS.md § Git & PR Policies), and CodeRabbit skips drafts by "
        "default, so a false value means no PR gets reviewed until it is readied."
    )
    pause = auto_review["auto_pause_after_reviewed_commits"]
    # Schema type is integer, and `== 0` alone would accept YAML `false` (bool
    # is an int subclass) or `0.0`, either of which CodeRabbit would reject.
    assert isinstance(pause, int) and not isinstance(pause, bool) and pause == 0, (
        "auto_pause_after_reviewed_commits must be integer 0 — drafts here "
        "iterate push -> CI -> fix and blow past CodeRabbit's default 5-commit "
        f"pause, which stops review with no signal. Found {pause!r}."
    )


def test_docstring_coverage_pre_merge_check_is_disabled() -> None:
    """Docstrings stay reviewable without enforcing a repository-wide quota."""
    pre_merge_checks = _config()["reviews"].get("pre_merge_checks")

    assert pre_merge_checks is not None, (
        "reviews.pre_merge_checks is missing — CodeRabbit would restore its "
        "default docstring coverage warning"
    )
    assert pre_merge_checks.get("docstrings", {}).get("mode") == "off", (
        "reviews.pre_merge_checks.docstrings.mode must be 'off' — this disables "
        "only the percentage-based coverage check, not ordinary docstring review"
    )


def test_bot_authors_stay_unreviewed() -> None:
    """The skip list is exact in both directions.

    Dropping a login floods that bot's PRs with reviews: nothing configured
    produces today's skip — the resolved config is entirely defaults — so it
    rests on undocumented CodeRabbit bot-author handling, and
    ``ignore_usernames`` defaults to ``[]``.

    Adding a login silently widens the policy — every PR that author opens
    loses automatic review. Each entry here is a recorded decision (see the
    ``BOT_AUTHORS`` comment); an addition must change both places, which is
    what forces the rationale to be written down.
    """
    configured = set(_config()["reviews"]["auto_review"]["ignore_usernames"])

    missing = BOT_AUTHORS - configured
    assert not missing, (
        f"ignore_usernames no longer covers {sorted(missing)} — those PRs would "
        "start getting reviewed. Matching is exact and case-sensitive, so the "
        "login must include the `[bot]` suffix."
    )
    extras = configured - BOT_AUTHORS
    assert not extras, (
        f"ignore_usernames gained {sorted(extras)} — every PR that login "
        "authors silently loses CodeRabbit review. If that is intended, add "
        "it to BOT_AUTHORS with the rationale recorded in the comment there."
    )


def test_codex_auto_skip_matches_the_coderabbit_ignore_list() -> None:
    """One auto-review skip policy, two enforcement points; no silent drift.

    The lists were unequal once — Codex admitted the webhook-proxy promote
    PRs that CodeRabbit ignored — and the divergence was invisible until a
    reviewer diffed the two files by hand. Only the ``pull_request_target``
    admission list must match: the ``issue_comment`` list stays narrower on
    purpose, so a maintainer's explicit `/review` on a promote PR still
    dispatches Codex (the on-demand lever the exclusion rationale relies on).
    """
    text = CODEX_REQUEST.read_text("utf-8")
    # Bound to the pull_request_target branch structurally, not by position.
    # Anchored on the event_name comparisons — the bare event names also
    # appear in the `on:` trigger block, which holds no skip list at all.
    auto_anchor = "github.event_name == 'pull_request_target'"
    comment_anchor = "github.event_name == 'issue_comment'"
    assert auto_anchor in text and comment_anchor in text, (
        f"{CODEX_REQUEST.name} no longer has the two admission branches this "
        "test expects — rebind the extraction below to the new structure."
    )
    auto_branch = text.split(auto_anchor, 1)[1].split(comment_anchor, 1)[0]
    raw_lists = [
        raw
        for raw in re.findall(r"fromJSON\('(\[[^']*\])'\)", auto_branch)
        if "[bot]" in raw
    ]
    assert len(raw_lists) == 1, (
        f"expected exactly one bot skip list in {CODEX_REQUEST.name}'s "
        f"pull_request_target branch, found {len(raw_lists)}"
    )

    auto_admission = set(json.loads(raw_lists[0]))
    assert auto_admission == BOT_AUTHORS, (
        f"{CODEX_REQUEST.name}'s auto-admission skip list {sorted(auto_admission)} "
        f"differs from .coderabbit.yaml's ignore_usernames {sorted(BOT_AUTHORS)} — "
        "the two tools would again disagree on which bot PRs get automatic review."
    )


def test_the_manual_review_escape_hatch_survives() -> None:
    """The `/review` comment path must keep admitting promote PRs.

    The auto-exclusion of ``github-actions[bot]`` is defensible only because a
    maintainer can still summon Codex on a promote PR with `/review`.
    Admission is a several-way conjunction; this test pins one piece of it —
    the ``issue_comment`` list omitting that login — because it is the piece
    that can die silently: the two ``fromJSON`` lists sit six lines apart and
    differ by one entry, so harmonising them reads as tidying. The other
    conjuncts (the trigger, the command literal, the author gate) are shared,
    repo-wide machinery whose breakage is loud on the next `/review` anywhere,
    and are deliberately not pinned here.
    """
    text = CODEX_REQUEST.read_text("utf-8")
    comment_anchor = "github.event_name == 'issue_comment'"
    assert comment_anchor in text, (
        f"{CODEX_REQUEST.name} no longer has an issue_comment branch — rebind "
        "this test to the new structure."
    )
    comment_branch = text.split(comment_anchor, 1)[1]
    raw_lists = [
        raw
        for raw in re.findall(r"fromJSON\('(\[[^']*\])'\)", comment_branch)
        if "[bot]" in raw
    ]
    assert len(raw_lists) == 1, (
        f"expected exactly one bot skip list in {CODEX_REQUEST.name}'s "
        f"issue_comment branch, found {len(raw_lists)}"
    )

    comment_skips = set(json.loads(raw_lists[0]))

    extras = comment_skips - COMMENT_PATH_SKIPS
    assert not extras, (
        f"{CODEX_REQUEST.name}'s issue_comment skip list gained {sorted(extras)} "
        "— that removes the manual /review lever on those authors' PRs, which "
        "for github-actions[bot] is the escape hatch the auto-exclusion "
        "rationale depends on."
    )
    missing = COMMENT_PATH_SKIPS - comment_skips
    assert not missing, (
        f"{CODEX_REQUEST.name}'s issue_comment skip list no longer covers "
        f"{sorted(missing)} — a maintainer /review on those bots' dependency "
        "PRs would start dispatching Codex."
    )


def test_agents_md_documents_the_real_auto_review_settings() -> None:
    """The prose is where a contributor learns the behaviour; pin it.

    Both settings, not just ``drafts``: the pause is the one carrying an
    operational cost (it spends the per-developer review allowance), so prose
    that documented only ``drafts`` described the cheaper half of the change.
    """
    section = _agents_md_subsection("Automated Code Review")
    auto_review = _config()["reviews"]["auto_review"]
    documented = {
        f"reviews.auto_review.drafts: {str(auto_review['drafts']).lower()}",
        f"auto_pause_after_reviewed_commits: "
        f"{auto_review['auto_pause_after_reviewed_commits']}",
    }
    missing = {claim for claim in documented if claim not in section}

    assert not missing, (
        "AGENTS.md § Automated Code Review must state the values "
        f"`.coderabbit.yaml` actually sets. Missing: {sorted(missing)}."
    )


def test_issue_enrichment_is_enabled() -> None:
    """Issue triage now rests on this block; the schema default is ``false``.

    CodeRabbit's docs say enrichment is "enabled by default", but the schema
    the file points at says ``auto_enrich.enabled`` defaults to ``false`` —
    and the schema is what applied in practice: no issue opened after the app
    install got an enrichment comment until this was set. With the GitHub
    Models triage bot deleted, a typo'd key here means new issues silently
    get no triage at all, from either system.
    """
    enrich = _config()["issue_enrichment"]

    assert enrich["auto_enrich"]["enabled"] is True, (
        "issue_enrichment.auto_enrich.enabled must be true — the schema "
        "default is false, and this block is the repo's only issue triage."
    )
    labeling = enrich["labeling"]
    assert labeling["auto_apply_labels"] is True, (
        "auto_apply_labels must be true — false (the default) demotes every "
        "labeling_instruction to a suggestion inside the enrichment comment."
    )
    instructions = labeling["labeling_instructions"]
    assert instructions, (
        "labeling_instructions is empty — auto-labeling has nothing to apply"
    )
    for entry in instructions:
        assert entry.get("label") and entry.get("instructions"), (
            f"labeling_instructions entry {entry!r} needs both `label` and "
            "`instructions`"
        )
        assert len(entry["instructions"]) <= 3000, (
            f"instructions for {entry['label']!r} exceed the schema's 3000-char "
            "maxLength — CodeRabbit rejects the config"
        )


def test_auto_planning_stays_off() -> None:
    """``auto_planning`` defaults to *enabled* with an empty label list.

    The docs define label matching only for non-empty lists, so what an empty
    list does with planning enabled is unspecified — and the failure mode is
    a full Coding Plan comment on every new issue. Plans are meant to be
    manual here (``@coderabbitai plan`` or the Create Plan checkbox), so the
    key must stay explicitly false.
    """
    planning = _config()["issue_enrichment"]["planning"]

    assert planning["auto_planning"]["enabled"] is False, (
        "issue_enrichment.planning.auto_planning.enabled must be false — "
        "removing it re-enables the schema default (true) with an empty label "
        "list, whose behaviour is undefined."
    )


def test_every_guideline_path_resolves() -> None:
    """A renamed styleguide would no-op the entry with no error anywhere."""
    patterns = _config()["knowledge_base"]["code_guidelines"]["filePatterns"]
    assert patterns, "filePatterns is empty — the styleguide entry went missing"

    for entry in patterns:
        files = entry["files"] if isinstance(entry, dict) else entry
        for pattern in (part.strip() for part in files.split(",")):
            assert list(REPO_ROOT.glob(pattern)), (
                f"`.coderabbit.yaml` points code_guidelines at {pattern!r}, "
                "which matches no file. A guideline path that resolves to "
                "nothing is silently ignored by CodeRabbit."
            )


def test_relocated_guidelines_apply_to_the_whole_tree() -> None:
    """Every delegated owner must retain repository-wide review coverage.

    Path resolution alone does not catch it: the entry keeps pointing at a real
    file while governing only its own subtree.
    """
    entries = {
        entry.get("files"): entry
        for entry in _config()["knowledge_base"]["code_guidelines"]["filePatterns"]
        if isinstance(entry, dict)
    }
    missing_guidelines = REPO_WIDE_GUIDELINES - entries.keys()
    assert not missing_guidelines, (
        "CodeRabbit is missing repo-wide guideline entries for "
        f"{sorted(missing_guidelines)}"
    )

    for guideline in sorted(REPO_WIDE_GUIDELINES):
        scope = {part.strip() for part in entries[guideline]["applyTo"].split(",")}
        missing = {p: why for p, why in REQUIRED_SCOPE.items() if p not in scope}

        assert not missing, (
            f"{guideline} no longer applies to: "
            + "; ".join(f"{why} (pattern {p!r})" for p, why in missing.items())
            + ". CodeRabbit drops those files from the guideline scope."
        )


def test_codex_review_request_points_at_a_real_styleguide() -> None:
    """The same path is hard-coded in the Codex request comment."""
    referenced = set(
        re.findall(
            rf"[\w./-]*{re.escape(STYLEGUIDE)}", CODEX_DELIVERY.read_text("utf-8")
        )
    )
    # The exact path, not merely something ending in it: a `vendor/` copy would
    # otherwise satisfy both this and the existence check below while the
    # workflow no longer pointed at the repo's own styleguide.
    assert STYLEGUIDE in referenced, (
        f"{CODEX_DELIVERY.name} no longer names {STYLEGUIDE} (found "
        f"{sorted(referenced)}) — if the review request moved, update this test "
        "alongside it."
    )
    for path in referenced:
        assert (REPO_ROOT / path).is_file(), (
            f"{CODEX_DELIVERY.name} points Codex at {path!r}, which does not exist."
        )
