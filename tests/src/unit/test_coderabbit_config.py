"""``.coderabbit.yaml`` must stay valid and match what AGENTS.md claims.

Nothing in CI parses this file — no yamllint, no schema check — and every key
it omits falls back to CodeRabbit's schema defaults rather than to any UI
setting. ``reviews.auto_review.drafts`` defaults to ``false``, so a typo'd key
or a bad indent silently restores the exact behaviour the file exists to
change, with no signal anywhere. The guideline paths have the same shape of
failure: rename ``.gemini/styleguide.md`` and both CodeRabbit and the Codex
review request point at nothing, quietly.

Same pattern as ``test_ruff_scope_covers_the_tree.py`` (pins a tool's config to
the tree) and ``test_locale_parity.py::test_agents_md_states_the_current_ceilings``
(pins AGENTS.md prose to the real values).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CODERABBIT_YAML = REPO_ROOT / ".coderabbit.yaml"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
CODEX_DELIVERY = REPO_ROOT / ".github" / "workflows" / "pr-codex-review-delivery.yml"
STYLEGUIDE = ".gemini/styleguide.md"

# ``applyTo`` has to keep the styleguide repo-wide, and minimatch's ``dot:
# false`` means each class of path needs its own pattern. Asserted separately
# rather than as one exact string: dropping any single one silently removes a
# whole class of files from the styleguide's scope, and that is the failure
# worth naming. Adding further patterns is fine.
# Bot identities whose PRs have never been reviewed. Exact logins: CodeRabbit
# matches ``ignore_usernames`` case-sensitively with no wildcard support.
BOT_AUTHORS = {
    "dependabot[bot]",
    "ha-mcp-renovate[bot]",
    "github-actions[bot]",
}

REQUIRED_SCOPE = {
    "**": "non-dot paths at any depth",
    "**/.*": "dotfiles at any depth, including the repo root",
    "**/.*/**": "contents of dot-directories (.github/, .claude/, .gemini/)",
}


def _config() -> dict[str, Any]:
    loaded = yaml.safe_load(CODERABBIT_YAML.read_text("utf-8"))
    assert isinstance(loaded, dict), f"{CODERABBIT_YAML.name} must parse to a mapping"
    return loaded


def _agents_md_subsection(title: str) -> str:
    """The body of one ``### `` subsection of AGENTS.md.

    Scoped rather than grepping the whole file, for the reason given in
    ``test_locale_parity.py::_agents_md_section``: a whole-file grep answers a
    question about the file, not about the section it claims to guard.
    """
    text = AGENTS_MD.read_text("utf-8")
    match = re.search(
        rf"^### {re.escape(title)}$(.*?)(?=^#{{2,3}} )", text, re.MULTILINE | re.DOTALL
    )
    assert match, f"AGENTS.md has no '### {title}' section — this test guards it"
    return match.group(1)


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


def test_bot_authors_stay_unreviewed() -> None:
    """Dependency and automation PRs were never reviewed; keep it that way.

    Nothing configured produces that today — the resolved config is entirely
    defaults — so it rests on undocumented CodeRabbit bot-author handling.
    Pinning the logins here makes the intent explicit and independent of that
    behaviour, and ``ignore_usernames`` defaults to ``[]``, so dropping the key
    would leave nothing standing between a handling change and a flood of
    reviews on every Dependabot, Renovate, and release-automation PR.
    """
    configured = set(_config()["reviews"]["auto_review"]["ignore_usernames"])
    missing = BOT_AUTHORS - configured

    assert not missing, (
        f"ignore_usernames no longer covers {sorted(missing)} — those PRs would "
        "start getting reviewed. Matching is exact and case-sensitive, so the "
        "login must include the `[bot]` suffix."
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


def test_styleguide_applies_to_the_whole_tree() -> None:
    """A narrowed ``applyTo`` scopes the styleguide to nothing, silently.

    Path resolution alone does not catch it: the entry keeps pointing at a real
    file while governing no reviewed code, because a guideline is otherwise
    scoped to its own directory tree and ``.gemini/`` holds no source.
    """
    entries = [
        entry
        for entry in _config()["knowledge_base"]["code_guidelines"]["filePatterns"]
        if isinstance(entry, dict) and entry.get("files") == STYLEGUIDE
    ]
    assert len(entries) == 1, (
        f"expected exactly one code_guidelines entry for {STYLEGUIDE}, found "
        f"{len(entries)} — a string entry would scope it to `.gemini/` only."
    )

    scope = {part.strip() for part in entries[0]["applyTo"].split(",")}
    missing = {p: why for p, why in REQUIRED_SCOPE.items() if p not in scope}

    assert not missing, (
        f"{STYLEGUIDE} no longer applies to: "
        + "; ".join(f"{why} (pattern {p!r})" for p, why in missing.items())
        + ". CodeRabbit drops those files from the styleguide's scope with no error."
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
