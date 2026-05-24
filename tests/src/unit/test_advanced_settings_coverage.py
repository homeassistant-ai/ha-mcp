"""Coverage gate (#1164).

Asserts every env-aliased Settings field is either:
  * in ``ADVANCED_SETTINGS_FIELDS`` (rendered in the Advanced section), OR
  * in ``FEATURE_FLAG_FIELDS`` (rendered in the Server Settings panel from
    PR #1381), OR
  * in ``BACKUP_OVERRIDE_FIELDS`` (rendered on the Backups tab from #1403), OR
  * on the explicit allow-list below.

When this test fails after a new env var is added, the contributor must
choose which registry to add it to — *not* extend the allow-list (the
allow-list is for things that genuinely have no place in the panel,
e.g. fields populated from package metadata).
"""

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
