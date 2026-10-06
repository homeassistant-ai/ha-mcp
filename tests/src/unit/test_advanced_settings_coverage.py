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
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic.fields import FieldInfo

from ha_mcp.config import (
    ADVANCED_SETTINGS_FIELDS,
    BACKUP_OVERRIDE_FIELDS,
    FEATURE_FLAG_FIELDS,
    Settings,
)
from ha_mcp.config_meta import AppOption, Setting
from ha_mcp.config_registry import _check_setting

_EN_CATALOG = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "ha_mcp"
    / "settings_ui"
    / "locales"
    / "en.json"
)

# Fields with no panel home by design. Adding to this list requires a
# one-line reason. Reviewer enforces.
ALLOWLIST: set[str] = {
    # DISABLED_TOOLS / PINNED_TOOLS are seed values for tool_config.json
    # managed via the Tool Visibility panel (separate tab), not via the
    # Advanced Settings or Feature Flags panels.
    "DISABLED_TOOLS",
    "PINNED_TOOLS",
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


@pytest.mark.parametrize(
    ("annotation", "setting", "problem"),
    [
        (str, Setting(range=(1, 10)), "a range on a non-numeric field"),
        (int, Setting(choices=("a", "b")), "choices on a non-str field"),
        (int, Setting(off_value=0), "an off value without a range"),
        (int, Setting(lenient=True), "lenient parsing with nothing to check"),
        (int, Setting(surface="advanced"), "an advanced setting without one"),
        (int, Setting(surface="feature", section="search"), "a section on"),
        (
            bool,
            Setting(surface="advanced", section="search", beta=True),
            "a beta flag that is not a bool feature flag",
        ),
        (
            bool,
            Setting(surface="feature", beta=True),
            "a beta flag that is not a dev-only app option",
        ),
        (
            bool,
            Setting(surface="feature", app=AppOption(flavors=("dev",))),
            "a dev-only app option that is not a beta flag",
        ),
        (float, Setting(app=AppOption()), "an app option of a type"),
        (list, Setting(surface="backup"), "a web UI surface on a type"),
    ],
)
def test_impossible_setting_metadata_fails_at_import(
    annotation: type, setting: Setting, problem: str
) -> None:
    """Metadata the registries and start.py cannot act on would be a silent
    no-op at runtime: a range nothing reads, a UI row rendered into no
    section, a beta flag the master gate cannot reach, an app option
    start.py cannot check. Import must fail instead."""
    field = FieldInfo(annotation=annotation, default=0, alias="SOME_ENV")

    with pytest.raises(RuntimeError, match=problem):
        _check_setting("some_field", field, setting)


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
# with a non-literal argument, as in ``config.py`` / ``settings_ui/__init__.py``)
# carry no literal to inspect and are correctly skipped — those iterate
# the registries themselves.

# Env vars deliberately read straight from the environment with no panel
# home. Each is a bind / secret / bootstrap / path value that must be set
# before (or independently of) the UI that would otherwise edit it.
ENV_ONLY: dict[str, str] = {
    "SUPERVISOR_TOKEN": "Injected by Supervisor; identifies add-on mode (secret/bootstrap)",
    "HA_MCP_EMBEDDED": "Set by the ha_mcp_tools in-process server entry before first import; identifies in-process mode (bootstrap)",
    "SUPERVISOR_BASE_URL": "Supervisor API base; bootstrap before any settings exist",
    "HAMCP_ENV_FILE": "Selects which .env file to load — read before Settings is built",
    "HA_MCP_CONFIG_DIR": "Resolves the data dir that *holds* the override files (path)",
    "XDG_DATA_HOME": "Root of the pre-#2372 auto-backup default; honoured only while snapshots remain there (path)",
    "HA_MCP_BUILD_VERSION": "Build metadata stamped at image build (bootstrap)",
    "HA_MCP_DISABLE_SETTINGS_UI": "Kill-switch for the settings sidecar itself (chicken-and-egg)",
    "MCP_HOST": "HTTP listener bind address — configured before the server is up",
    "MCP_PORT": "HTTP listener port — configured before the server is up",
    "MCP_HTTP_PORT": "HTTP listener port (alt name) — bind config",
    "MCP_BASE_URL": "Externally advertised base URL — bind/deployment config",
    "MCP_SECRET_PATH": "Secret URL path component for the MCP endpoint (secret)",
    "MCP_SETTINGS_SECRET_PATH": "Dedicated secret URL path for the settings UI in OAuth/OIDC modes; read at startup before the server is up (secret/bind config)",
    "MCP_HEALTHZ": "Opt-in /healthz liveness route — bind/deployment config",
    "FASTMCP_PORT": "FastMCP transport bind port",
    "FASTMCP_TRANSPORT": "FastMCP transport selection — bootstrap",
    "OIDC_CONFIG_URL": "OIDC provider discovery URL — OIDC auth mode bootstrap (pre-Settings)",
    "OIDC_CLIENT_ID": "OIDC OAuth client ID — OIDC auth mode bootstrap (pre-Settings)",
    "OIDC_CLIENT_SECRET": "OIDC OAuth client secret — OIDC auth mode bootstrap (pre-Settings)",
    "OIDC_JWT_SIGNING_KEY": "Optional JWT signing key for persistent OIDC sessions — OIDC mode only",
    "OIDC_ALLOWED_CLIENT_REDIRECT_URIS": "Optional allow-list of dynamically-registered client redirect URIs — OIDC mode only",
    "OIDC_VERIFY_ID_TOKEN": "Opt-in ID-token verification for opaque-access-token OIDC providers — OIDC mode only",
    "OIDC_AUDIENCE": "Optional expected `aud` claim for IdP-issued access tokens — OIDC mode only",
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
    os_aliases, environ_aliases, getenv_aliases = _resolve_env_aliases(tree)
    return _collect_env_reads(tree, os_aliases, environ_aliases, getenv_aliases)


def _resolve_env_aliases(
    tree: ast.Module,
) -> tuple[set[str], set[str], set[str]]:
    """Resolve the per-file ``os`` / ``environ`` / ``getenv`` aliases, including
    the ``from os import ...`` and ``as``-aliased variants."""
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
    return os_aliases, environ_aliases, getenv_aliases


def _is_environ_ref(
    node: ast.expr, os_aliases: set[str], environ_aliases: set[str]
) -> bool:
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


def _is_getenv_ref(
    node: ast.expr, os_aliases: set[str], getenv_aliases: set[str]
) -> bool:
    # ``os.getenv`` or a bare ``getenv`` imported via ``from os import``.
    if isinstance(node, ast.Name):
        return node.id in getenv_aliases
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "getenv"
        and isinstance(node.value, ast.Name)
        and node.value.id in os_aliases
    )


def _collect_env_reads(
    tree: ast.Module,
    os_aliases: set[str],
    environ_aliases: set[str],
    getenv_aliases: set[str],
) -> set[str]:
    """Collect every literal env var read through the resolved ``os`` /
    ``environ`` / ``getenv`` aliases in ``tree``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, ast.Call):
            fn = node.func
            if _is_getenv_ref(fn, os_aliases, getenv_aliases) or (
                isinstance(fn, ast.Attribute)
                and fn.attr in {"get", "setdefault", "pop"}
                and _is_environ_ref(fn.value, os_aliases, environ_aliases)
            ):
                name = _first_literal(node.args)
        elif isinstance(node, ast.Subscript) and _is_environ_ref(
            node.value, os_aliases, environ_aliases
        ):
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
        rel = py.relative_to(pkg_dir).as_posix()
        if rel.startswith(("_vendor/", "resources/skills-vendor/")):
            # Vendored code (websockets, the skills submodule): its env
            # knobs (WEBSOCKETS_*, the skills eval scripts') are upstream's
            # interface, not ha-mcp settings to surface in the Settings UI.
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
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


@pytest.mark.parametrize(
    ("section", "fields"),
    [("advanced", ADVANCED_SETTINGS_FIELDS), ("backup.fields", BACKUP_OVERRIDE_FIELDS)],
)
def test_every_settings_row_has_an_english_label_and_help(
    section: str, fields: Sequence[Any]
) -> None:
    """A row without ``<section>.<field>.label`` and ``.help`` in ``en.json``
    renders its raw snake_case field name and no help text: the settings
    script has no other English copy to fall back on."""
    english = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))["messages"]
    missing = sorted(
        f"{section}.{f.field}.{part}"
        for f in fields
        for part in ("label", "help")
        if not english.get(f"{section}.{f.field}.{part}")
    )
    assert not missing, f"en.json lacks these settings row strings: {missing}"


def test_screenshot_engine_url_is_surfaced_as_editable_advanced_field() -> None:
    """#1538: the screenshot engine URL is env-settable (docker/.env) but was
    invisible to add-on users. It must now be an editable Advanced row, no
    longer on the env-only ALLOWLIST."""
    row = next(
        (
            r
            for r in ADVANCED_SETTINGS_FIELDS
            if r.field == "dashboard_screenshot_engine_url"
        ),
        None,
    )
    assert row is not None, "dashboard_screenshot_engine_url must be an advanced field"
    assert row.ftype is str
    assert row.editable is True
    assert "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL" not in ALLOWLIST


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


@pytest.mark.asyncio
async def test_get_advanced_tells_the_ui_which_saves_need_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UI shows the restart banner from this flag. The log level is set
    once at startup; the screenshot engine URL is read per capture."""
    from unittest.mock import MagicMock

    from ha_mcp.config import _reset_global_settings
    from ha_mcp.settings_ui import build_settings_handlers

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    _reset_global_settings()
    handlers = build_settings_handlers(server=None)
    body = json.loads((await handlers["get_advanced_settings"](MagicMock())).body)
    rows = {row["field"]: row for row in body["fields"]}

    assert rows["log_level"]["restart_required"] is True
    assert rows["dashboard_screenshot_engine_url"]["restart_required"] is False
