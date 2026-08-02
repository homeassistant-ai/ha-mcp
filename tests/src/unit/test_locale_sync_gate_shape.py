"""Guard the shape of the locale-sync workflow's completeness gate.

The completeness checks in ``test_locale_parity.py`` and
``test_settings_ui_i18n.py`` skip unless ``LOCALE_COMPLETENESS_CHECKS`` is
set, and the only place that sets it is ``.github/workflows/locale-sync.yml``
— so the env-var linkage is a merge-safety control with the same fail-open
shape ``test_e2e_skip_gate_shape.py`` guards: rename either side and pytest
exits 0 with every gated test skipped, and the workflow pushes unverified
machine translations to master, green, forever. The workflow's own junit
skip-count assertion catches that at run time; these tests catch it before
merge, the only coverage a workflow that cannot run pre-merge can have.

Also pinned: the push job must not land a clean run whose CONTENT
verification failed, must not be blocked by the non-blocking staleness
signal, and must confine the patch to the locale allowlist.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "locale-sync.yml"
_GATED_TEST_FILES = (
    Path(__file__).with_name("test_locale_parity.py"),
    Path(__file__).with_name("test_settings_ui_i18n.py"),
)


def _workflow() -> dict[str, Any]:
    data: Any = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{_WORKFLOW} did not parse to a mapping"
    return data


def _marker_env_names() -> set[str]:
    """Every env var a completeness skipif marker reads, from the sources."""
    names: set[str] = set()
    for path in _GATED_TEST_FILES:
        source = path.read_text(encoding="utf-8")
        names.update(
            re.findall(
                r"pytest\.mark\.skipif\(\s*not os\.environ\.get\(\"([A-Z_]+)\"\)",
                source,
            )
        )
    return names


def _verification_steps() -> list[dict[str, Any]]:
    """The translate-job steps that run the gated suites."""
    steps = _workflow()["jobs"]["translate"]["steps"]
    return [
        step
        for step in steps
        if "pytest" in str(step.get("run", ""))
        and (
            "test_locale_parity.py" in str(step.get("run", ""))
            or "test_settings_ui_i18n.py" in str(step.get("run", ""))
        )
    ]


def test_the_markers_agree_on_one_env_var() -> None:
    """Both files must gate on the same variable, or the workflow misses one."""
    assert _marker_env_names() == {"LOCALE_COMPLETENESS_CHECKS"}, (
        f"completeness markers read {_marker_env_names()} — update this test, "
        "the workflow env blocks, and both marker definitions together"
    )


def test_every_verification_step_sets_the_gate_env_var() -> None:
    """The env linkage is the one edit that fails open; pin both sides."""
    steps = _verification_steps()
    assert len(steps) >= 2, (
        f"expected the content and staleness verification steps in "
        f"{_WORKFLOW.name}, found {len(steps)} — the gated suites no longer "
        "run anywhere"
    )
    (env_name,) = _marker_env_names()
    for step in steps:
        env = step.get("env") or {}
        assert str(env.get(env_name, "")) not in ("", "0"), (
            f"{_WORKFLOW.name} step {step.get('name')!r} runs a gated suite "
            f"without setting {env_name} — every gated test would skip and "
            "pytest would exit 0 having verified nothing"
        )


def test_the_k_filters_name_real_tests() -> None:
    """A renamed test turns a ``-k`` filter into an empty selection."""
    for step in _verification_steps():
        run = str(step.get("run", ""))
        for match in re.finditer(r'-k "(?:not )?(\w+)"', run):
            name = match.group(1)
            assert any(
                name in path.read_text(encoding="utf-8") for path in _GATED_TEST_FILES
            ), (
                f"{_WORKFLOW.name} step {step.get('name')!r} selects "
                f"{name!r}, which no gated test file defines — the filter "
                "selects or excludes nothing"
            )


def test_push_blocks_on_content_failure_and_not_on_staleness() -> None:
    """The push predicate is the whole safety story; pin its three clauses."""
    condition = str(_workflow()["jobs"]["push"]["if"])
    assert "always()" in condition, (
        "the push job must use always() or a failed translate job's partial "
        "progress never lands (the resumability story)"
    )
    assert "needs.translate.outputs.have_patch == 'true'" in condition, (
        "the push job must require an exported patch"
    )
    assert (
        "needs.translate.outputs.translate_outcome == 'success'" in condition
        and "needs.translate.outputs.verify_outcome == 'failure'" in condition
    ), (
        "the push job must refuse a clean run whose content verification "
        "failed — that is the only gate between corrupt machine output and "
        "master"
    )
    assert "staleness" not in condition, (
        "the push job must NOT consult the staleness outcome — a held "
        "feature-gated stub key is routine and blocking on it wedges every "
        "unrelated translation while re-burning the Gemini quota daily"
    )


def test_the_push_allowlist_covers_exactly_the_pipeline_outputs() -> None:
    """Every path the export stages must be one the push job accepts.

    The export step's ``git add`` list and the push job's case-statement
    allowlist are the same contract written twice; a path added to one side
    only either never lands (export-only) or is dead weight (allowlist-only).
    """
    jobs = _workflow()["jobs"]
    export = "\n".join(str(step.get("run", "")) for step in jobs["translate"]["steps"])
    allowlist = "\n".join(str(step.get("run", "")) for step in jobs["push"]["steps"])
    for path in (
        "src/ha_mcp/settings_ui/locales",
        "src/ha_mcp/settings_ui/settings.js",
        "custom_components/ha_mcp_tools/translations",
        "homeassistant-addon/translations",
        "homeassistant-addon-dev/translations",
        "tests/src/unit/locale_source_baseline.json",
        "tests/src/unit/locale_sync_progress.json",
    ):
        assert path in export, (
            f"the export step no longer stages {path!r} — pipeline output "
            "there would silently never land"
        )
        assert path in allowlist, (
            f"the push job's allowlist no longer accepts {path!r} — the "
            "patch would be refused"
        )


def test_dispatch_on_a_non_default_ref_fails_loudly() -> None:
    """A wrong-ref dispatch must go red, not silently skip green."""
    steps = _workflow()["jobs"]["translate"]["steps"]
    guard = "\n".join(
        str(step.get("run", ""))
        for step in steps
        if "default_branch" in str(step.get("run", ""))
    )
    assert "exit 1" in guard, (
        f"{_WORKFLOW.name} no longer refuses workflow_dispatch on a "
        "non-default ref — translating an arbitrary tree and pushing it to "
        "master must fail visibly"
    )
