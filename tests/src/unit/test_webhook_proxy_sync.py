"""Unit tests for the webhook-proxy promote/reset transform engine.

These exercise the pure helpers of ``scripts/webhook_proxy_sync.py`` without
touching the filesystem: the semver bump math, the monotonic dev-reset rule
(including the case where stable's base is *behind* dev's), and the assertion
that the STABLE and DEV identity profiles are exact inverses of each other.

The full round-trip (that ``reset``/``promote`` produce a version-only diff on
a clean tree) is guaranteed by the acceptance gate in the build spec, not here.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNC_PATH = REPO_ROOT / "scripts" / "webhook_proxy_sync.py"


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


def test_committed_dev_equals_reset_transform_of_stable(tmp_path):
    """Drift guard: the committed dev tree MUST equal reset(stable) — the
    identity transform applied to the committed stable tree — modulo the version
    lines. Any silent divergence between the two hand-maintained copies (a stable
    fix not mirrored into dev, or vice-versa) fails CI here. Strictly stronger
    than the token-only contamination test, which only checks that no bare
    `mcp_proxy` token leaks into the dev tree.
    """
    import re
    import shutil

    # Stage both committed addon trees + the ruff config into a scratch root and
    # run the stable -> dev transform there (never touching the real worktree).
    for flavor in (sync.STABLE, sync.DEV):
        shutil.copytree(
            REPO_ROOT / flavor.addon_dir,
            tmp_path / flavor.addon_dir,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    shutil.copy2(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    sync.sync("reset", root=tmp_path)

    committed = REPO_ROOT / sync.DEV.addon_dir
    produced = tmp_path / sync.DEV.addon_dir
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
            if p.is_file() and "__pycache__" not in p.parts
        }

    committed_files = files(committed)
    produced_files = files(produced)
    assert set(committed_files) == set(produced_files), (
        "dev tree file set differs from transform(stable): "
        f"{set(committed_files) ^ set(produced_files)}"
    )
    drifted = [
        str(rel)
        for rel in sorted(committed_files)
        if norm(committed_files[rel]) != norm(produced_files[rel])
    ]
    assert not drifted, (
        "committed dev tree has drifted from transform(stable) — regenerate it "
        f"with the reset workflow (scripts/webhook_proxy_sync.py): {drifted}"
    )
