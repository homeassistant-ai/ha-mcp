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
