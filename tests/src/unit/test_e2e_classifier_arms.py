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
from functools import cache
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"

# Workflows carrying a copy of the classifier. Every corpus below is asserted
# against BOTH: they are hand-duplicated, so a fix applied to one can miss the
# other — #1712 added the app-markdown arm to haos-e2e-tests.yml alone, and the
# same file ran the pr.yml matrix while skipping the haos one for months.
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
    # User-facing markdown inside the app / component dirs. Its arm has to
    # sit ahead of the app-dirs arm, which would otherwise claim it, so this
    # pins the ordering as much as the patterns.
    "homeassistant-addon/DOCS.md",
    "homeassistant-addon-dev/DOCS.md",
    "homeassistant-addon/README.md",
    "homeassistant-addon/CHANGELOG.md",
    "homeassistant-addon-webhook-proxy/DOCS.md",
    "custom_components/ha_mcp_tools/README.md",
)

# Non-markdown files in the app / bake-input trees. They are baked into the
# qcow2 or shipped, so they still count as code on both copies.
_ADDON_CODE = (
    "homeassistant-addon/config.yaml",
    "homeassistant-addon-dev/Dockerfile",
    "tests/haos_image_build/build.sh",
    "custom_components/ha_mcp_tools/const.py",
)


@cache
def _case_arms(workflow: str) -> str:
    """The classifier step's `case ... esac` block, with its `$f` loop variable.

    Cached: this is a pure file read, and the corpora below would otherwise
    reparse two workflows of a thousand-plus lines once per path.
    """
    data = yaml.safe_load((_WORKFLOW_DIR / workflow).read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job.get("steps") or ():
            if step.get("id") == "filter" and "run" in step:
                match = re.search(
                    r"^\s*case \"\$f\" in$.*?^\s*esac$", str(step["run"]), re.M | re.S
                )
                assert match, f'{workflow}: no `case "$f" in` block in the classifier'
                return match.group(0)
    raise AssertionError(f"{workflow}: no classifier step with id 'filter'")


def _classify(workflow: str, path: str) -> str:
    """Run the workflow's own `case` arms over one path; return run= verdict.

    The arms themselves are replayed under the real shell rather than
    reimplemented, so glob semantics (notably `*` crossing `/`) come from the
    shell that runs them in CI, not from this test's idea of them. The
    arms run inside a one-iteration loop so their `break` and `continue` are
    legal exactly as in production; each path is classified alone, so
    short-circuiting across a multi-path changeset is not covered.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - CI images all ship bash
        pytest.skip("bash unavailable")
    program = (
        f'run=false\nfor f in "$1"; do\n{_case_arms(workflow)}\ndone\n'
        'printf %s "$run"\n'
    )
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
@pytest.mark.parametrize("path", _ADDON_CODE)
def test_app_directories_still_count_as_code(workflow: str, path: str) -> None:
    """Markdown in those trees skips; everything else there is baked or shipped."""
    assert _classify(workflow, path) == "true", (
        f"{workflow} classifies {path} as docs, but the app trees are baked "
        "into the qcow2 / shipped, so their non-markdown files are code."
    )


@pytest.mark.parametrize("workflow", _CLASSIFIER_WORKFLOWS)
def test_issue_template_arm_is_pinned_to_the_form_extension(workflow: str) -> None:
    """The arm must name *.yml, not the whole directory.

    Pinned as text as well as by behaviour: `test_code_paths_never_skip_the_suite`
    catches a bare `ISSUE_TEMPLATE/*` via the nested-path corpus entries, but
    this states the requirement where the next editor of the arm will read it.
    """
    block = _case_arms(workflow)
    assert ".github/ISSUE_TEMPLATE/*.yml" in block, (
        f"{workflow}: the issue-form arm must be pinned to *.yml — `*` crosses "
        "`/` in a case pattern, so ISSUE_TEMPLATE/* would also skip a future "
        "non-form file in that directory."
    )
