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
verification failed, must not land a partial run whose LITERAL parity
failed, must not be blocked by the non-blocking staleness signal, and must
confine the patch to the locale allowlist.
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
    Path(__file__).with_name("test_translate_locales.py"),
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


# The one verification step that must stay UNGATED. It runs on the partial
# tree the completeness step cannot judge, and the arm it selects carries no
# completeness marker — so the gate variable is inert there today: the ``-k``
# filter deselects everything that reads it. The assertion below still earns
# its place, because it pairs with the step's own ``skipped="0"`` check to
# make a widened filter fail loudly rather than quietly drag in tests that
# cannot pass on a half-finished tree.
_UNGATED_STEP_ID = "verify_partial"


def _verification_steps() -> list[dict[str, Any]]:
    """Every translate-job step that runs one of the locale suites.

    Gated and ungated alike — the caller splits them two lines later.
    """
    steps = _workflow()["jobs"]["translate"]["steps"]
    return [
        step
        for step in steps
        if "pytest" in str(step.get("run", ""))
        and any(path.name in str(step.get("run", "")) for path in _GATED_TEST_FILES)
    ]


def test_the_markers_agree_on_one_env_var() -> None:
    """Both files must gate on the same variable, or the workflow misses one."""
    assert _marker_env_names() == {"LOCALE_COMPLETENESS_CHECKS"}, (
        f"completeness markers read {_marker_env_names()} — update this test, "
        "the workflow env blocks, and both marker definitions together"
    )


def test_every_verification_step_sets_the_gate_env_var() -> None:
    """The env linkage is the one edit that fails open; pin both sides.

    Both directions are asserted. A gated step that loses the variable
    verifies nothing and exits 0. The partial-run step is asserted not to set
    it for a narrower reason: the variable is inert there while the ``-k``
    filter holds, so this pins the pair — variable absent, filter narrow —
    rather than a runtime effect it has today.
    """
    steps = _verification_steps()
    gated = [step for step in steps if step.get("id") != _UNGATED_STEP_ID]
    ungated = [step for step in steps if step.get("id") == _UNGATED_STEP_ID]
    assert len(gated) >= 2, (
        f"expected the content and staleness verification steps in "
        f"{_WORKFLOW.name}, found {len(gated)} — the gated suites no longer "
        "run anywhere"
    )
    assert len(ungated) == 1, (
        f"expected exactly one ungated verification step (id "
        f"{_UNGATED_STEP_ID!r}) in {_WORKFLOW.name}, found {len(ungated)} — "
        "a partial run would land its patch unverified"
    )
    (env_name,) = _marker_env_names()
    for step in gated:
        env = step.get("env") or {}
        assert str(env.get(env_name, "")) not in ("", "0"), (
            f"{_WORKFLOW.name} step {step.get('name')!r} runs a gated suite "
            f"without setting {env_name} — every gated test would skip and "
            "pytest would exit 0 having verified nothing"
        )
    for step in ungated:
        env = step.get("env") or {}
        assert env_name not in env, (
            f"{_WORKFLOW.name} step {step.get('name')!r} must NOT set "
            f"{env_name}: it runs against a partial tree, where the gated "
            "tests cannot pass. The -k filter keeps them out today, so the "
            "variable would be inert — but the two are one control, and a "
            "later widening of the filter must not find the gate already open"
        )


def test_the_partial_step_runs_on_the_partial_path() -> None:
    """The condition that decides whether the step runs at all.

    Everything else about this step is pinned -- its id, its absent gate env,
    its ``-k`` filter, the push clause that reads its outcome -- and none of
    that survives the one edit that matters. Flip ``!=`` to ``==``, a
    plausible copy from either neighbouring step, and the step never runs on
    the path it exists for: its outcome becomes ``skipped``, which satisfies
    the push job's ``!= 'failure'``, and the unverified tree lands with every
    test in this file still green.
    """
    steps = _workflow()["jobs"]["translate"]["steps"]
    (step,) = [s for s in steps if s.get("id") == _UNGATED_STEP_ID]
    condition = " ".join(str(step.get("if", "")).split())
    assert "steps.translate.outcome != 'success'" in condition, (
        f"the {_UNGATED_STEP_ID!r} step no longer runs when the translate "
        f"step did NOT succeed — its condition is {condition!r}. On the "
        "partial path it would skip, and a skipped outcome passes the push "
        "job's gate, so the unverified patch lands"
    )
    assert "steps.export.outputs.have_patch == 'true'" in condition, (
        f"the {_UNGATED_STEP_ID!r} step must require an exported patch, or an "
        "earlier hard failure reddens it over a tree nothing would push"
    )
    assert "!cancelled()" in condition, (
        f"the {_UNGATED_STEP_ID!r} step needs !cancelled(): a step whose "
        "predecessor failed is skipped by default, which is the whole path "
        "this one covers"
    )


def test_the_push_gate_reads_outcomes_the_job_actually_publishes() -> None:
    """An outcome the job never exports reads as the empty string.

    The push condition compares three job outputs. Drop one from the
    ``outputs:`` block and GitHub resolves the expression to ``''`` — which
    is ``!= 'failure'`` — so the clause is satisfied for every run, including
    the one whose verification went red. The push predicate is asserted in
    full elsewhere; this is the other half of that contract.
    """
    translate = _workflow()["jobs"]["translate"]
    outputs = translate.get("outputs") or {}
    step_ids = {str(step.get("id")) for step in translate["steps"] if step.get("id")}
    condition = " ".join(str(_workflow()["jobs"]["push"]["if"]).split())
    consumed = set(re.findall(r"needs\.translate\.outputs\.(\w+)", condition))
    missing = consumed - set(outputs)
    assert not missing, (
        f"the push job reads {sorted(missing)} from the translate job, which "
        f"does not export {'it' if len(missing) == 1 else 'them'} — the "
        "expression resolves to the empty string, and every comparison "
        "against 'failure' passes"
    )
    tolerant = {
        str(step["id"]) for step in translate["steps"] if step.get("continue-on-error")
    }
    for name in sorted(consumed):
        referenced = re.findall(r"steps\.(\w+)\.(\w+)", str(outputs[name]))
        assert all(ref in step_ids for ref, _ in referenced), (
            f"the translate job's {name!r} output reads "
            f"{[ref for ref, _ in referenced]}, which names no step in the "
            f"job — it would publish the empty string"
        )
        # The field carries as much as the step id. Under continue-on-error a
        # step's `conclusion` is permanently 'success', so reading that
        # instead of `outcome` satisfies every `!= 'failure'` clause in the
        # push predicate — the red tree pushes, and an id-only assertion sees
        # nothing, because the id resolves either way.
        for ref, field in referenced:
            assert ref not in tolerant or field == "outcome", (
                f"the translate job's {name!r} output reads {ref}.{field}, "
                f"but {ref} runs under continue-on-error, where conclusion is "
                f"always 'success' — the push gate would never see a failure"
            )


def _selected_test_names(run: str) -> set[str]:
    """Every test name a ``-k`` expression in this step names.

    Reading only ``-k "name"`` misses every compound filter, and a compound
    filter is how a test gets excluded from a run: matching the quoted
    expression as a whole and pulling the identifiers out of it is what keeps
    ``-k "not a and not b"`` from selecting nothing to check.
    """
    names: set[str] = set()
    for expression in re.findall(r'-k "([^"]+)"', run):
        names.update(
            token
            for token in re.findall(r"[A-Za-z_]\w*", expression)
            if token not in {"and", "or", "not"}
        )
    return names


def test_the_k_filters_name_real_tests() -> None:
    """A renamed test turns a ``-k`` filter into an empty selection."""
    for step in _verification_steps():
        for name in sorted(_selected_test_names(str(step.get("run", "")))):
            assert any(
                name in path.read_text(encoding="utf-8") for path in _GATED_TEST_FILES
            ), (
                f"{_WORKFLOW.name} step {step.get('name')!r} selects "
                f"{name!r}, which no gated test file defines — the filter "
                "selects or excludes nothing"
            )


def test_the_partial_step_selects_the_literal_parity_test() -> None:
    """The step that guards a partial push must select the check by name.

    Nothing else pins which test the partial path runs. Repointed at any
    other test in the file the step still collects, still skips nothing, and
    still passes — and the tree pushes with no catalog compared.
    """
    partial = next(
        step
        for step in _verification_steps()
        if "partial" in str(step.get("name", "")).lower()
    )
    assert "test_translations_keep_english_numbers_and_identifiers" in (
        _selected_test_names(str(partial.get("run", "")))
    ), (
        f"{_WORKFLOW.name} step {partial.get('name')!r} no longer selects the "
        "literal-parity check — the partial path would verify nothing and the "
        "push gate would read a green skip"
    )


def test_no_step_excludes_the_literal_parity_test() -> None:
    """Excluding the check is how it stops running without anything failing.

    The clean path's filter is a ``not`` expression, and appending one more
    term to it removes literal parity from the successful run while every
    other assertion in this file — the step ids, the gate env, the push
    clause, even the name check above — stays green.
    """
    for step in _verification_steps():
        run = " ".join(str(step.get("run", "")).split())
        for expression in re.findall(r'-k "([^"]+)"', run):
            assert not re.search(
                r"\bnot\s+test_translations_keep_english_numbers_and_identifiers\b",
                expression,
            ), (
                f"{_WORKFLOW.name} step {step.get('name')!r} excludes the "
                f"literal-parity check in {expression!r} — that run would "
                "verify everything except the catalogs"
            )


def test_push_blocks_on_content_failure_and_not_on_staleness() -> None:
    """The push predicate is the whole safety story; pin its three clauses.

    The blocking clause is asserted as the full NEGATED expression, not its
    fragments: dropping the ``!`` inverts the safety property — pushing only
    the corrupt case — while every fragment still appears verbatim.
    """
    condition = " ".join(str(_workflow()["jobs"]["push"]["if"]).split())
    assert "always()" in condition, (
        "the push job must use always() or a failed translate job's partial "
        "progress never lands (the resumability story)"
    )
    assert "needs.translate.outputs.have_patch == 'true'" in condition, (
        "the push job must require an exported patch"
    )
    assert (
        "!(needs.translate.outputs.translate_outcome == 'success' && "
        "needs.translate.outputs.verify_outcome == 'failure')" in condition
    ), (
        "the push job must refuse — negation included — a clean run whose "
        "content verification failed; that is the only gate between corrupt "
        "machine output and master"
    )
    assert "needs.translate.outputs.verify_partial_outcome != 'failure'" in condition, (
        "the push job must refuse a partial run whose literal parity failed — "
        "the completeness step is skipped on that path, and a backfilled key "
        "is never re-queued once it exists, so landing a dropped identifier "
        "freezes it"
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
    The allowlist is compared as an exact SET, so a widened arm — admitting
    paths the export never legitimately stages — fails just like a dropped
    one. settings.js is deliberately absent from both sides: its generated
    block derives from en.json alone, so the sync never changes it, and its
    absence is what keeps executable code off the patch entirely.
    """
    jobs = _workflow()["jobs"]
    # Only the step that stages the patch: the verify steps also name
    # tests/src/unit paths, which would satisfy the assertion even with the
    # git add list gutted.
    export = "\n".join(
        str(step.get("run", ""))
        for step in jobs["translate"]["steps"]
        if "git add" in str(step.get("run", ""))
    )
    push_run = "\n".join(str(step.get("run", "")) for step in jobs["push"]["steps"])
    labels = {
        match.group(1)
        for match in re.finditer(r"^\s*([^\s\"$(]+)\)\s*(?:;;)?\s*$", push_run, re.M)
    }
    assert labels == {
        "src/ha_mcp/settings_ui/locales/*",
        "custom_components/ha_mcp_tools/translations/*",
        "homeassistant-addon/translations/*",
        "homeassistant-addon-dev/translations/*",
        "tests/src/unit/locale_source_baseline.json",
        "tests/src/unit/locale_sync_progress.json",
        "*",
    }, (
        f"the push job's allowlist arms are {sorted(labels)} — they must be "
        "exactly the pipeline's data-only outputs plus the refusing "
        "catch-all; a widened or narrowed arm changes what the bypass "
        "credential will push"
    )
    # The arm SET says the catch-all exists; this says it still refuses — a
    # gutted `*) ;;` keeps the set identical while admitting every path,
    # and that exit 1 is the entire allowlist.
    assert re.search(
        r"\*\)\s*\n\s*echo \"::error::[^\"]*outside the locale allowlist"
        r"[^\"]*\"\s*\n\s*exit 1",
        push_run,
    ), "the catch-all arm no longer refuses — the allowlist admits every path"
    for path in (
        "src/ha_mcp/settings_ui/locales",
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
    assert "settings.js" not in export, (
        "the export step stages settings.js — the one executable file the "
        "old allowlist carried; the sync never legitimately changes it (its "
        "generated block derives from en.json alone), and staging it "
        "reopens the code-smuggling channel"
    )


def test_the_translate_checkout_persists_no_credential() -> None:
    """The translate job's no-credential claim is structural, so pin it.

    ``permissions: contents: read`` already keeps a persisted GITHUB_TOKEN
    from pushing; what this backs is the narrower property the workflow
    header states outright — the job that executes the resolved dependency
    tree holds no ambient credential at all beyond the Gemini key.
    """
    steps = _workflow()["jobs"]["translate"]["steps"]
    checkouts = [
        step for step in steps if "actions/checkout" in str(step.get("uses", ""))
    ]
    assert checkouts, "the translate job no longer checks out the repository"
    for step in checkouts:
        assert (step.get("with") or {}).get("persist-credentials") is False, (
            "the translate job's checkout persists a credential — the job "
            "that executes the resolved dependency tree must hold nothing "
            "beyond the Gemini key"
        )


def test_dispatch_on_a_non_default_ref_fails_loudly() -> None:
    """A wrong-ref dispatch must go red, not silently skip green.

    The actual comparison is pinned, not just the ``exit 1``: a guard whose
    condition was hollowed out (``if false``) still carries the exit.
    """
    steps = _workflow()["jobs"]["translate"]["steps"]
    # The ref and default branch reach the script through env (never
    # expanded into shell), so match on the whole step, not just its run.
    guard = "\n".join(
        str(step.get("run", "")) for step in steps if "default_branch" in str(step)
    )
    assert '"$GITHUB_REF" != "refs/heads/${DEFAULT_BRANCH}"' in guard, (
        f"{_WORKFLOW.name} no longer compares the FULL dispatched ref "
        "against the default branch — a tag named after the branch would "
        "push its older tree to master"
    )
    assert "exit 1" in guard, (
        f"{_WORKFLOW.name} no longer refuses workflow_dispatch on a "
        "non-default ref — translating an arbitrary tree and pushing it to "
        "master must fail visibly"
    )


def test_verification_steps_consume_their_junit_reports() -> None:
    """Every junit report written must be checked for silent skips.

    Deleting the ``assert_nothing_skipped`` call sites (or the staleness
    step's inline check) would leave pytest's exit code as the only signal,
    which is 0 when every gated test skipped — the fail-open hole the
    reports exist to close.
    """
    for step in _verification_steps():
        run = str(step.get("run", ""))
        reports = run.count("--junitxml")
        assert reports > 0, (
            f"step {step.get('name')!r} no longer writes a junit report — "
            "nothing detects an all-skipped (vacuously green) suite"
        )
        if "assert_nothing_skipped" in run:
            consumed = len(re.findall(r'assert_nothing_skipped "', run))
        else:
            consumed = reports if 'skipped="0"' in run else 0
        assert consumed == reports, (
            f"step {step.get('name')!r} writes {reports} junit report(s) "
            f"but checks {consumed} — an unchecked report is the fail-open "
            "hole again"
        )
