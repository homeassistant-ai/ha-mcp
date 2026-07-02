"""Unit tests for the webhook-proxy promote/reset transform engine.

These exercise the pure helpers of ``scripts/webhook_proxy_sync.py`` without
touching the filesystem: the semver bump math, the monotonic dev-reset rule
(including the case where stable's base is *behind* dev's), and the assertion
that the STABLE and DEV identity profiles are exact inverses of each other.

The full round-trip (that ``reset``/``promote`` produce a version-only diff on
a clean tree) is guaranteed by the acceptance gate in the build spec, not here.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNC_PATH = REPO_ROOT / "scripts" / "webhook_proxy_sync.py"

# The committed-tree drift guards below are SYNC-POINT invariants, not
# development-time invariants: PRs land on the DEV flavor only (the stable
# guard blocks direct stable edits), so dev intentionally runs AHEAD of stable
# between promotes, and the two committed trees are only supposed to be
# identity-equal right after the promote/reset transform has run. Those
# workflows set WEBHOOK_PROXY_TREES_SYNCED=1 and run this file as the
# transform's acceptance gate; in regular per-PR CI the drift guards skip
# (every pure transform-engine test above them still runs everywhere).
_sync_point_only = pytest.mark.skipif(
    os.environ.get("WEBHOOK_PROXY_TREES_SYNCED") != "1",
    reason="committed-tree drift guards apply only at promote/reset sync "
    "points (WEBHOOK_PROXY_TREES_SYNCED=1); the dev flavor intentionally "
    "runs ahead of stable between promotes",
)


def _load_sync():
    spec = importlib.util.spec_from_file_location("webhook_proxy_sync", SYNC_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass can resolve its own
    # __module__ (dataclasses looks it up in sys.modules during processing).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # safe: module-level has no side effects
    return mod


sync = _load_sync()


def _version_key(version: str) -> tuple[int, int, int, float]:
    """Sortable key matching the dev version-guard's ordering."""
    base, dev_n = (
        (sync.parse_dev_version(version))
        if ".dev" in version
        else (sync.parse_stable_version(version), float("inf"))
    )
    return (*base, dev_n)


# ---------------------------------------------------------------------------
# Promote: semver bump math
# ---------------------------------------------------------------------------
def test_promote_bump_patch():
    assert sync.promote_version("1.2.3", "patch") == "1.2.4"
    assert sync.promote_version("1.2.9", "patch") == "1.2.10"
    assert sync.promote_version("0.0.0", "patch") == "0.0.1"


def test_promote_bump_minor():
    assert sync.promote_version("1.2.3", "minor") == "1.3.0"
    assert sync.promote_version("1.0.9", "minor") == "1.1.0"


def test_promote_bump_major():
    assert sync.promote_version("1.2.3", "major") == "2.0.0"
    assert sync.promote_version("9.9.9", "major") == "10.0.0"


def test_bump_stable_tuple_math():
    assert sync.bump_stable((1, 2, 3), "patch") == (1, 2, 4)
    assert sync.bump_stable((1, 2, 3), "minor") == (1, 3, 0)
    assert sync.bump_stable((1, 2, 3), "major") == (2, 0, 0)


# ---------------------------------------------------------------------------
# Reset: dev version must ALWAYS strictly increase
# ---------------------------------------------------------------------------
def test_reset_bumps_dev_counter_when_bases_equal():
    assert sync.reset_version("1.2.3.dev1", "1.2.3") == "1.2.3.dev2"


def test_reset_takes_stable_base_when_stable_is_ahead():
    # stable ahead of dev's base -> base climbs, counter still advances
    assert sync.reset_version("1.0.0.dev3", "1.5.0") == "1.5.0.dev4"


def test_reset_still_increases_when_stable_base_is_behind_dev():
    # The key case: stable's code is BEHIND dev's base. The version must still
    # strictly increase (max-base keeps the higher base; devN advances).
    new = sync.reset_version("2.0.0.dev5", "1.5.0")
    assert new == "2.0.0.dev6"
    assert _version_key(new) > _version_key("2.0.0.dev5")
    # base must not regress to stable's lower (1.5.0) base
    assert new.startswith("2.0.0.")


def test_reset_monotonic_across_a_range_of_pairs():
    pairs = [
        ("1.2.3.dev1", "1.2.3"),
        ("1.0.0.dev9", "1.5.0"),
        ("2.0.0.dev5", "1.5.0"),
        ("1.2.9.dev2", "1.2.3"),
        ("3.4.5.dev1", "3.4.5"),
        ("0.1.0.dev1", "0.0.9"),
    ]
    for current_dev, current_stable in pairs:
        new = sync.reset_version(current_dev, current_stable)
        assert _version_key(new) > _version_key(current_dev), (
            f"reset({current_dev}, {current_stable}) = {new} did not increase"
        )


# ---------------------------------------------------------------------------
# The STABLE and DEV identity profiles are exact inverses
# ---------------------------------------------------------------------------
def test_profiles_are_exact_inverses():
    assert sync.profiles_are_inverse(sync.STABLE, sync.DEV)
    assert sync.profiles_are_inverse(sync.DEV, sync.STABLE)


def test_inverse_swappable_identity_fields():
    stable, dev = sync.STABLE, sync.DEV
    # each flavor's slug is the other's sibling-slug-base
    assert stable.slug == dev.sibling_slug_base
    assert dev.slug == stable.sibling_slug_base
    # each flavor's own mutex id is the other's sibling-mutex id
    assert stable.mutex_notification_id == dev.sibling_mutex_notification_id
    assert dev.mutex_notification_id == stable.sibling_mutex_notification_id
    # flavor/sibling words cross over
    assert stable.flavor_word.lower() == dev.sibling_word
    assert dev.flavor_word.lower() == stable.sibling_word
    # the component token differs by exactly the "_dev" suffix
    assert dev.component == stable.component + "_dev"


def test_profiles_not_inverse_of_themselves():
    assert not sync.profiles_are_inverse(sync.STABLE, sync.STABLE)
    assert not sync.profiles_are_inverse(sync.DEV, sync.DEV)


# ---------------------------------------------------------------------------
# Spot-checks on the pure text transforms (round-trip inverse behavior)
# ---------------------------------------------------------------------------
def test_rename_tokens_reset_collapses_doubled_suffix():
    # reset direction: mcp_proxy -> mcp_proxy_dev, with a pre-existing
    # mcp_proxy_dev literal that must not become mcp_proxy_dev_dev.
    src = 'A = "mcp_proxy_mutex"\nB = "mcp_proxy_dev_mutex"\n'
    out = sync.rename_tokens(src, "mcp_proxy", "mcp_proxy_dev")
    assert out == 'A = "mcp_proxy_dev_mutex"\nB = "mcp_proxy_dev_mutex"\n'
    assert "mcp_proxy_dev_dev" not in out


def test_rename_tokens_promote_is_plain_substitution():
    src = 'x = "/opt/mcp_proxy_dev"\ny = "mcp_proxy_mutex"\n'
    out = sync.rename_tokens(src, "mcp_proxy_dev", "mcp_proxy")
    assert out == 'x = "/opt/mcp_proxy"\ny = "mcp_proxy_mutex"\n'


def test_config_yaml_stage_added_for_dev_removed_for_stable():
    stable_cfg = (
        'name: "x"\ndescription: "y"\nversion: "1.2.3"\n'
        'slug: "s"\nurl: "u"\narch:\n  - amd64\nboot: auto\n'
    )
    dev_out = sync.transform_config_yaml(stable_cfg, sync.DEV, "1.2.3.dev2")
    lines = dev_out.split("\n")
    assert 'slug: "ha_mcp_webhook_proxy_dev"' in lines
    assert "boot: manual" in lines
    assert "stage: experimental" in lines
    # stage sits directly after the url line, matching the dev layout
    assert lines[lines.index("stage: experimental") - 1].startswith("url:")

    # promoting that dev config back to stable drops the stage line
    stable_out = sync.transform_config_yaml(dev_out, sync.STABLE, "1.2.4")
    assert "stage: experimental" not in stable_out.split("\n")
    assert "boot: auto" in stable_out.split("\n")


# Files that legitimately differ between the two hand-maintained flavors and are
# NOT part of the identity transform, so the drift guards below compare them
# against neither flavor's transform output. Hand-maintained allowlist — every
# code / identity file (the component dir, start.py, Dockerfile, config.yaml,
# manifest.json, translations/en.yaml) IS transformed and MUST NOT appear here;
# a real identity-token miss must be reconciled, not hidden by padding this set.
# Only docs live here:
#   * AGENTS.md    — maintainer notes; the full doc lives in the stable tree,
#                    the dev tree carries a hand-written pointer stub to it
#   * CLAUDE.md    — per-flavor symlink alias of that flavor's AGENTS.md
#   * CHANGELOG.md — release-pipeline-synced per flavor, independent content
#   * DOCS.md      — generated add-on docs, independent per-flavor content
_DRIFT_ALLOWLIST = frozenset({"AGENTS.md", "CLAUDE.md", "CHANGELOG.md", "DOCS.md"})


def _assert_committed_equals_transform(tmp_path, direction, src, dst, bump=None):
    """Seed the produced tree from the SOURCE flavor, run the identity
    transform, and assert the result equals the committed DESTINATION tree for
    every file except the hand-maintained doc allowlist (version lines
    normalized).

    Seeding the produced (destination) tree from the committed SOURCE — not the
    destination — is the crux of the guard: any file the transform SHOULD emit
    but doesn't touch then remains the untransformed source verbatim and
    mismatches the committed destination, surfacing an identity file left out of
    the transform (as ``translations/en.yaml`` once was). Seeding from the
    destination would compare such a file to itself and hide the drift.
    """
    import re
    import shutil

    src_committed = REPO_ROOT / src.addon_dir
    dst_committed = REPO_ROOT / dst.addon_dir

    # Stage the committed SOURCE tree twice — as the transform's input (src dir)
    # AND as the produced tree's seed (dst dir) — plus the ruff config, in a
    # scratch root; never touches the real worktree.
    shutil.copytree(
        src_committed,
        tmp_path / src.addon_dir,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        src_committed,
        tmp_path / dst.addon_dir,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # The component dir is RENAMED (mcp_proxy <-> mcp_proxy_dev) and rebuilt
    # wholesale by the transform, so the source-named copy the seed left in the
    # destination dir is a pure seeding artifact — drop it so the transform
    # produces the destination component dir cleanly. Its files are still fully
    # compared, under the destination name.
    shutil.rmtree(tmp_path / dst.addon_dir / src.component)
    # config.yaml IS transformed (rebuilt from the source), but sync() first
    # reads the DESTINATION's current version out of it, and that version must
    # parse in the destination's format (dev ``X.Y.Z.devN`` vs stable
    # ``X.Y.Z``). Restore the committed destination config.yaml solely so that
    # read succeeds; the transform overwrites it and the comparison version-
    # normalizes it, so this hides nothing.
    shutil.copy2(
        dst_committed / "config.yaml", tmp_path / dst.addon_dir / "config.yaml"
    )
    shutil.copy2(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")

    sync.sync(direction, bump=bump, root=tmp_path)

    produced = tmp_path / dst.addon_dir
    ver_re = re.compile(r'("?version"?:\s*")[^"]+(")')

    def norm(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        if path.name in ("config.yaml", "manifest.json"):
            text = ver_re.sub(r"\1<VERSION>\2", text)
        return text

    def files(root: Path) -> dict:
        return {
            p.relative_to(root): p
            for p in root.rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.name not in _DRIFT_ALLOWLIST
        }

    committed_files = files(dst_committed)
    produced_files = files(produced)
    assert set(committed_files) == set(produced_files), (
        f"{dst.addon_dir} file set differs from transform({src.addon_dir}) "
        f"(allowlist {sorted(_DRIFT_ALLOWLIST)} excluded): "
        f"{set(committed_files) ^ set(produced_files)}"
    )
    drifted = [
        str(rel)
        for rel in sorted(committed_files)
        if norm(committed_files[rel]) != norm(produced_files[rel])
    ]
    assert not drifted, (
        f"committed {dst.addon_dir} tree has drifted from "
        f"transform({src.addon_dir}) — regenerate it with "
        f"scripts/webhook_proxy_sync.py: {drifted}"
    )


@_sync_point_only
def test_committed_dev_equals_reset_transform_of_stable(tmp_path):
    """Sync-point drift guard: the committed dev tree MUST equal reset(stable) — the
    identity transform applied to the committed stable tree — modulo the version
    lines and the hand-maintained doc allowlist. Any silent divergence between
    the two hand-maintained copies (a stable fix not mirrored into dev, or an
    identity file left out of the transform) fails CI here. Strictly stronger
    than the token-only contamination test: the produced tree is seeded from the
    SOURCE (stable) flavor, so a file the transform forgets to emit surfaces as
    the untransformed source verbatim instead of comparing equal to itself.
    """
    _assert_committed_equals_transform(tmp_path, "reset", sync.STABLE, sync.DEV)


@_sync_point_only
def test_committed_stable_equals_promote_transform_of_dev(tmp_path):
    """Sync-point reverse drift guard: the committed stable tree MUST equal promote(dev) —
    the inverse identity transform applied to the committed dev tree — modulo
    the version lines and the hand-maintained doc allowlist. Mirrors the reset
    drift guard for the promote direction, confirming the reverse transform
    restores the stable mutex constants, drops `stage:`, and applies the stable
    identity/labels. A dev-only change not mirrored back into stable (or a broken
    inverse) fails CI here. Seeded from the SOURCE (dev) flavor so a dropped
    identity file surfaces rather than comparing equal to itself.
    """
    _assert_committed_equals_transform(
        tmp_path, "promote", sync.DEV, sync.STABLE, bump="patch"
    )
