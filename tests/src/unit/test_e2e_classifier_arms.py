"""Pin the decisions the e2e change classifier's ``case`` arms actually make.

``test_e2e_skip_gate_shape.py`` guards the *gate predicates* — that every lane
consults ``needs.changes.result`` and demands an explicit ``false`` before
honoring a skip. It never reads the classifier's ``run:`` script, so the
``case`` arms that decide *whether* ``run`` becomes ``false`` are unguarded.
Those arms are the other half of the same merge-safety control: branch
protection reads a skipped required check as Success, so an arm that is broader
than intended skips the suite and reports green.

Two ways an arm goes wrong, neither visible in review:

1. ``*`` crosses ``/`` in a POSIX ``case`` pattern, so ``.github/FOO/*`` also
   matches ``.github/FOO/nested/deep.py``. A pattern meant for one file type
   silently covers anything dropped in that directory later.
2. The arms are order-sensitive and an arm inserted above the bake-inputs arm
   shadows it.

These tests extract each workflow's classifier script and run it under the real
shell against a corpus of paths, so they assert the decision rather than the
text. They pass on arrival and fire when an arm's reach changes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"

# Workflows carrying a copy of the classifier. Both are checked independently:
# they are hand-duplicated, so a fix applied to one can miss the other.
_CLASSIFIER_WORKFLOWS = ("pr.yml", "haos-e2e-tests.yml")

# Paths that must NEVER skip the suite. The classifier is fail-closed by
# design, and these are the ways a widened arm would breach that floor.
_MUST_RUN = (
    "src/ha_mcp/server.py",
    "tests/src/unit/test_e2e_classifier_arms.py",
    "uv.lock",
    "pyproject.toml",
    ".github/workflows/pr.yml",
    ".github/dependabot.yml",
    ".coderabbit.yaml",
    "custom_components/ha_mcp_tools/const.py",
    # `*` crosses `/`, so the ISSUE_TEMPLATE arm must be pinned to the form
    # extension or a non-form file in that directory would skip the suite.
    ".github/ISSUE_TEMPLATE/nested/deep.py",
    ".github/ISSUE_TEMPLATE/helper.py",
)

# Paths that legitimately skip the suite on BOTH copies of the classifier.
_MUST_SKIP = (
    "README.md",
    "docs/FAQ.md",
    "docs/agents/development.md",
    "site/src/pages/faq.astro",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/runtime_bug.yml",
    ".github/ISSUE_TEMPLATE/startup_bug.yml",
)


def _classifier_script(workflow: str) -> str:
    """Return the `Classify changed files` step's shell script."""
    data = yaml.safe_load((_WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job.get("steps") or ():
            if step.get("id") == "filter" and "run" in step:
                return str(step["run"])
    raise AssertionError(f"{workflow}: no classifier step with id 'filter'")


def _case_block(script: str) -> str:
    """Extract just the `case ... esac` block, with its `$f` loop variable."""
    match = re.search(r"^\s*case \"\$f\" in$.*?^\s*esac$", script, re.M | re.S)
    assert match, 'classifier no longer contains a `case "$f" in` block'
    return match.group(0)


def _classify(workflow: str, path: str) -> str:
    """Run the workflow's own `case` arms over one path; return run= verdict.

    The arms are replayed verbatim under the real shell rather than
    reimplemented, so glob semantics (notably `*` crossing `/`) come from the
    shell that runs them in CI, not from this test's idea of them.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - CI images all ship bash
        pytest.skip("bash unavailable")
    program = f'run=false\nf="$1"\n{_case_block(_classifier_script(workflow))}\nprintf %s "$run"\n'
    result = subprocess.run(
        [bash, "-c", program, "bash", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("workflow", _CLASSIFIER_WORKFLOWS)
@pytest.mark.parametrize("path", _MUST_RUN)
def test_code_paths_never_skip_the_suite(workflow: str, path: str) -> None:
    """Anything that is not docs must leave run=true (fail-closed floor)."""
    assert _classify(workflow, path) == "true", (
        f"{workflow} classifies {path} as docs, so the required e2e checks skip "
        "and report Success without running."
    )


@pytest.mark.parametrize("workflow", _CLASSIFIER_WORKFLOWS)
@pytest.mark.parametrize("path", _MUST_SKIP)
def test_documentation_paths_skip_the_suite(workflow: str, path: str) -> None:
    """Docs, website, and GitHub issue forms do not warrant the suite."""
    assert _classify(workflow, path) == "false", (
        f"{workflow} classifies {path} as code, so a documentation-only change "
        "runs the full e2e suite for nothing."
    )


@pytest.mark.parametrize("workflow", _CLASSIFIER_WORKFLOWS)
def test_issue_template_arm_is_pinned_to_the_form_extension(workflow: str) -> None:
    """The arm must name *.yml, not the whole directory.

    Pinned as text as well as by behaviour: `test_code_paths_never_skip_the_suite`
    catches a bare `ISSUE_TEMPLATE/*` via the nested-path corpus entries, but
    this states the requirement where the next editor of the arm will read it.
    """
    block = _case_block(_classifier_script(workflow))
    assert ".github/ISSUE_TEMPLATE/*.yml" in block, (
        f"{workflow}: the issue-form arm must be pinned to *.yml — `*` crosses "
        "`/` in a case pattern, so ISSUE_TEMPLATE/* would also skip a future "
        "non-form file in that directory."
    )
