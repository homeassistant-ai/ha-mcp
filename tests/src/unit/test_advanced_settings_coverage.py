"""Coverage gate for env-aliased Settings fields.

Asserts every env-aliased Settings field is either:
  * in ``ADVANCED_SETTINGS_FIELDS`` (rendered in the Advanced section), OR
  * in ``FEATURE_FLAG_FIELDS`` (rendered in the Server Settings panel), OR
  * in ``BACKUP_OVERRIDE_FIELDS`` (rendered on the Backups tab), OR
  * on the explicit allow-list below.

When this test fails after a new env var is added, the contributor must
choose which registry to add it to — *not* extend the allow-list (the
allow-list is for things that genuinely have no place in the panel,
e.g. fields populated from package metadata).
"""

import ast
import sys
from pathlib import Path

from ha_mcp.config import (
    ADVANCED_SETTINGS_FIELDS,
    BACKUP_OVERRIDE_FIELDS,
    FEATURE_FLAG_FIELDS,
    Settings,
)

# Fields with no panel home by design. Adding to this list requires a
# one-line reason. Reviewer enforces.
ALLOWLIST: set[str] = {
    # DISABLED_TOOLS / PINNED_TOOLS are seed values for tool_config.json
    # managed via the Tool Visibility panel (separate tab), not via the
    # Advanced Settings or Feature Flags panels.
    "DISABLED_TOOLS",
    "PINNED_TOOLS",
    # Screenshot-engine URL is a deployment-environment connection string
    # (the Docker/Container sidecar path), not a user-facing UI toggle — it
    # is deliberately env/.env-only and absent from every override registry.
    # See the comment on dashboard_screenshot_engine_url in config.py.
    "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
}


def _all_env_aliases() -> set[str]:
    aliases: set[str] = set()
    for field in Settings.model_fields.values():
        alias = field.alias
        if alias is None:
            continue
        aliases.add(alias)
    return aliases


def _registered_aliases() -> set[str]:
    aliases: set[str] = set()
    for _name, env, *_ in ADVANCED_SETTINGS_FIELDS:
        aliases.add(env)
    for _name, env, _t in FEATURE_FLAG_FIELDS:
        aliases.add(env)
    for _name, env, _t in BACKUP_OVERRIDE_FIELDS:
        aliases.add(env)
    return aliases


def test_every_env_aliased_setting_is_surfaced_or_allowlisted() -> None:
    aliases = _all_env_aliases()
    registered = _registered_aliases()
    missing = aliases - registered - ALLOWLIST
    assert not missing, (
        f"New Settings env vars without a panel home: {sorted(missing)}. "
        "Add them to ADVANCED_SETTINGS_FIELDS, FEATURE_FLAG_FIELDS, or "
        "BACKUP_OVERRIDE_FIELDS, or document in ALLOWLIST with a reason."
    )


def test_advanced_section_values_are_in_known_set() -> None:
    """Section strings are consumed by the UI to pick a render
    container. A typo (e.g. ``"connetion"``) would silently render the
    row into nothing. Lock the closed set."""
    known = {
        "connection",
        "search",
        "operations",
        "diagnostics",
        "tools_surface",
        "beta_codemode",
    }
    seen = {row[3] for row in ADVANCED_SETTINGS_FIELDS}
    bad = seen - known
    assert not bad, (
        f"ADVANCED_SETTINGS_FIELDS has unknown section values: {sorted(bad)}. "
        f"Known set: {sorted(known)}."
    )


def test_validate_registries_rejects_phantom_field(monkeypatch) -> None:
    """``_validate_registries`` must raise when a row references a
    Settings field that does not exist on the model. Confidence-add for
    the validator function itself; the production registries pass it on
    every import, so this proves the *negative* path.
    """
    import pytest

    from ha_mcp import config as cfg

    phantom = cfg.FeatureFlagField("field_that_does_not_exist", "PHANTOM_ENV", bool)
    patched = (*cfg.FEATURE_FLAG_FIELDS, phantom)
    monkeypatch.setattr(cfg, "FEATURE_FLAG_FIELDS", patched)
    with pytest.raises(RuntimeError, match="references fields not on Settings"):
        cfg._validate_registries()


def test_validate_registries_rejects_overlap_between_registries(monkeypatch) -> None:
    """Two registries can't both apply the same field — they'd coerce
    via potentially divergent type policies."""
    import pytest

    from ha_mcp import config as cfg

    # Pick a real flag field and inject it into BACKUP_OVERRIDE_FIELDS too.
    flag = cfg.FEATURE_FLAG_FIELDS[0]
    dupe = cfg.BackupOverrideField(flag.field, "DUPE_ENV", flag.ftype)
    patched = (*cfg.BACKUP_OVERRIDE_FIELDS, dupe)
    monkeypatch.setattr(cfg, "BACKUP_OVERRIDE_FIELDS", patched)
    with pytest.raises(RuntimeError, match="Registry overlap between"):
        cfg._validate_registries()


def test_validate_registries_rejects_bounds_on_non_numeric_field(
    monkeypatch,
) -> None:
    """``_ADVANCED_SETTINGS_BOUNDS`` entries must point at numeric advanced
    fields — a bounds rule on a bool/str field is a programming error."""
    import pytest

    from ha_mcp import config as cfg

    # Find a real str-typed advanced field and pretend bounds apply to it.
    str_field = next(f for f in cfg.ADVANCED_SETTINGS_FIELDS if f.ftype is str)
    patched_bounds = {**cfg._ADVANCED_SETTINGS_BOUNDS, str_field.field: (1, 10)}
    monkeypatch.setattr(cfg, "_ADVANCED_SETTINGS_BOUNDS", patched_bounds)
    with pytest.raises(RuntimeError, match="non-numeric"):
        cfg._validate_registries()


def test_validate_registries_rejects_choices_on_non_string_field(monkeypatch) -> None:
    """``_ADVANCED_SETTINGS_CHOICES`` entries must point at str advanced
    fields — choices on a numeric/bool field is a programming error."""
    import pytest

    from ha_mcp import config as cfg

    int_field = next(f for f in cfg.ADVANCED_SETTINGS_FIELDS if f.ftype is int)
    patched_choices = {
        **cfg._ADVANCED_SETTINGS_CHOICES,
        int_field.field: ("a", "b"),
    }
    monkeypatch.setattr(cfg, "_ADVANCED_SETTINGS_CHOICES", patched_choices)
    with pytest.raises(RuntimeError, match="non-str"):
        cfg._validate_registries()


def test_validate_registries_rejects_beta_field_not_in_feature_flags(
    monkeypatch,
) -> None:
    """Every BETA_FEATURE_FIELDS entry must also be in FEATURE_FLAG_FIELDS
    — the master gate writes through ``setattr`` so a phantom beta name
    would silently land on extras with no runtime effect."""
    import pytest

    from ha_mcp import config as cfg

    patched_beta = (*cfg.BETA_FEATURE_FIELDS, "phantom_beta_field")
    monkeypatch.setattr(cfg, "BETA_FEATURE_FIELDS", patched_beta)
    with pytest.raises(RuntimeError, match="BETA_FEATURE_FIELDS contains names not in"):
        cfg._validate_registries()


# ---------------------------------------------------------------------------
# Guard against env-var drift bypassing config.py (issue #1538)
# ---------------------------------------------------------------------------
#
# The coverage gate above only sees env vars that are ``Settings`` fields.
# A tunable knob read directly via ``os.environ`` / ``os.getenv`` never
# becomes a Settings field, so it stays invisible to the web Settings UI
# and unreachable for add-on users. That is exactly how the three
# ``HAMCP_*_TIME_BUDGET`` knobs drifted out of the panel before #1538.
#
# This guard scans the shipped source for *direct, string-literal* env
# reads and requires each to be either a registered ``Settings`` alias
# (and therefore covered by the gate above) or on the explicit
# ``ENV_ONLY`` list below. It resolves the relevant import aliases per
# file, so both the attribute forms (``os.environ[...]`` / ``os.getenv``)
# and the ``from os import environ, getenv`` forms — including ``as``
# aliases — are caught. Registry-driven reads (``os.environ.get(var)``
# with a non-literal argument, as in ``config.py`` / ``settings_ui.py``)
# carry no literal to inspect and are correctly skipped — those iterate
# the registries themselves.

# Env vars deliberately read straight from the environment with no panel
# home. Each is a bind / secret / bootstrap / path value that must be set
# before (or independently of) the UI that would otherwise edit it.
ENV_ONLY: dict[str, str] = {
    "SUPERVISOR_TOKEN": "Injected by Supervisor; identifies add-on mode (secret/bootstrap)",
    "SUPERVISOR_BASE_URL": "Supervisor API base; bootstrap before any settings exist",
    "HAMCP_ENV_FILE": "Selects which .env file to load — read before Settings is built",
    "HA_MCP_CONFIG_DIR": "Resolves the data dir that *holds* the override files (path)",
    "XDG_DATA_HOME": "Data-dir fallback root (path)",
    "HA_MCP_BUILD_VERSION": "Build metadata stamped at image build (bootstrap)",
    "HA_MCP_DISABLE_SETTINGS_UI": "Kill-switch for the settings sidecar itself (chicken-and-egg)",
    "MCP_HOST": "HTTP listener bind address — configured before the server is up",
    "MCP_PORT": "HTTP listener port — configured before the server is up",
    "MCP_HTTP_PORT": "HTTP listener port (alt name) — bind config",
    "MCP_BASE_URL": "Externally advertised base URL — bind/deployment config",
    "MCP_SECRET_PATH": "Secret URL path component for the MCP endpoint (secret)",
    "FASTMCP_PORT": "FastMCP transport bind port",
    "FASTMCP_TRANSPORT": "FastMCP transport selection — bootstrap",
}


def _package_dir() -> Path:
    cfg = sys.modules["ha_mcp.config"]
    assert cfg.__file__ is not None  # regular module always has __file__
    return Path(cfg.__file__).resolve().parent


def _first_literal(args: list[ast.expr]) -> str | None:
    if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
        return args[0].value
    return None


def _env_reads_in_tree(tree: ast.Module) -> set[str]:
    """Resolve per-file aliases for the ``os`` module and the ``environ`` /
    ``getenv`` names, then collect every literal env var read through them."""
    os_aliases: set[str] = set()  # names bound to the ``os`` module
    environ_aliases: set[str] = set()  # names bound to ``os.environ``
    getenv_aliases: set[str] = set()  # names bound to ``os.getenv``
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    environ_aliases.add(alias.asname or "environ")
                elif alias.name == "getenv":
                    getenv_aliases.add(alias.asname or "getenv")

    def _is_environ(node: ast.expr) -> bool:
        # ``os.environ`` (attribute on the os module) or a bare ``environ``
        # imported via ``from os import environ``.
        if isinstance(node, ast.Name):
            return node.id in environ_aliases
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
        )

    def _is_getenv(node: ast.expr) -> bool:
        # ``os.getenv`` or a bare ``getenv`` imported via ``from os import``.
        if isinstance(node, ast.Name):
            return node.id in getenv_aliases
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "getenv"
            and isinstance(node.value, ast.Name)
            and node.value.id in os_aliases
        )

    names: set[str] = set()
    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, ast.Call):
            fn = node.func
            if _is_getenv(fn) or (
                isinstance(fn, ast.Attribute)
                and fn.attr in {"get", "setdefault", "pop"}
                and _is_environ(fn.value)
            ):
                name = _first_literal(node.args)
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                name = sl.value
        if name is not None:
            names.add(name)
    return names


def _literal_env_reads() -> dict[str, set[str]]:
    """Map ``ENV_VAR -> {relative source paths}`` for every direct
    string-literal ``os.environ`` / ``os.getenv`` read in the package."""
    pkg_dir = _package_dir()
    found: dict[str, set[str]] = {}
    for py in pkg_dir.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        rel = py.relative_to(pkg_dir).as_posix()
        for name in _env_reads_in_tree(tree):
            found.setdefault(name, set()).add(rel)
    return found


def test_no_unregistered_direct_env_reads() -> None:
    """Every direct ``os.environ`` read of a literal var name must be a
    registered ``Settings`` alias or an explicitly documented ENV_ONLY var."""
    reads = _literal_env_reads()
    aliases = _all_env_aliases()
    offenders = {
        name: sorted(paths)
        for name, paths in reads.items()
        if name not in aliases and name not in ENV_ONLY
    }
    assert not offenders, (
        "Direct os.environ reads of unregistered env vars (invisible to the "
        f"Settings UI): {offenders}. Promote each to a Settings field (and a "
        "registry: ADVANCED_SETTINGS_FIELDS / FEATURE_FLAG_FIELDS / "
        "BACKUP_OVERRIDE_FIELDS), or add it to ENV_ONLY with a one-line reason."
    )


def test_env_only_list_has_no_dead_entries() -> None:
    """Keep ENV_ONLY honest — an entry no longer read anywhere (and not a
    Settings alias) is dead and should be removed so the list documents
    reality."""
    reads = _literal_env_reads()
    aliases = _all_env_aliases()
    dead = sorted(
        name for name in ENV_ONLY if name not in reads and name not in aliases
    )
    assert not dead, f"ENV_ONLY lists vars no longer read in src: {dead}"


def test_scanner_detects_all_direct_read_forms() -> None:
    """Guard the guard: the scanner must catch every direct-read form,
    including the ``from os import ...`` and ``as``-aliased variants, and
    must skip non-literal (registry-driven) reads."""
    src = (
        "import os\n"
        "import os as _os\n"
        "from os import environ, getenv\n"
        "from os import environ as _env, getenv as _ge\n"
        "a = os.environ['A_ATTR_SUBSCRIPT']\n"
        "b = os.environ.get('B_ATTR_GET')\n"
        "c = os.getenv('C_ATTR_GETENV')\n"
        "d = _os.environ['D_OS_ALIAS']\n"
        "e = environ['E_FROM_SUBSCRIPT']\n"
        "f = environ.get('F_FROM_GET')\n"
        "g = getenv('G_FROM_GETENV')\n"
        "h = _env.get('H_FROM_ALIAS')\n"
        "i = _ge('I_GETENV_ALIAS')\n"
        "os.environ.setdefault('J_SETDEFAULT', 'x')\n"
        "m = os.environ.pop('M_POP', None)\n"
        "k = os.environ.get(some_var)\n"  # non-literal -> skipped
    )
    assert _env_reads_in_tree(ast.parse(src)) == {
        "A_ATTR_SUBSCRIPT",
        "B_ATTR_GET",
        "C_ATTR_GETENV",
        "D_OS_ALIAS",
        "E_FROM_SUBSCRIPT",
        "F_FROM_GET",
        "G_FROM_GETENV",
        "H_FROM_ALIAS",
        "I_GETENV_ALIAS",
        "J_SETDEFAULT",
        "M_POP",
    }


def test_advanced_registries_are_name_disjoint() -> None:
    """Each override-file key must be applied by exactly one of
    _apply_feature_flag_overrides or _apply_advanced_overrides. A
    field listed in both would be coerced twice with potentially
    divergent policies (e.g. bool branch vs str branch)."""
    advanced = {row[0] for row in ADVANCED_SETTINGS_FIELDS}
    flags = {row[0] for row in FEATURE_FLAG_FIELDS}
    backup = {row[0] for row in BACKUP_OVERRIDE_FIELDS}
    overlap = (advanced & flags) | (advanced & backup) | (flags & backup)
    assert not overlap, (
        f"Settings field name appears in multiple registries: {sorted(overlap)}. "
        "Each field must be in exactly one of ADVANCED_SETTINGS_FIELDS, "
        "FEATURE_FLAG_FIELDS, or BACKUP_OVERRIDE_FIELDS."
    )
