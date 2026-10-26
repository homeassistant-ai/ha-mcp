"""Operator-listed app option keys under redact_secrets (issue 2843).

Some apps ship a secret as a plain string option with no ``format: password``
in their schema; ``redact_app_option_keys`` lets the operator name those
options so they get the same handling as schema password fields.
"""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.redaction import (
    REDACTED_SET,
    collect_addon_secret_values,
    known_secret_values,
    redact_addon_options,
)
from ha_mcp.tools.tools_addons import AddOnTools, get_addon_info

from .test_redaction import _clean_registry as _clean_registry

BACKUP_SLUG = "1a2b3c4d_hassio_google_drive_backup"
BACKUP_PASSWORD = "drive-backup-encryption-pw"

BACKUP_SCHEMA = [
    {"name": "backup_password", "type": "string", "optional": True},
    {"name": "days_between_backups", "type": "integer"},
    {
        "name": "upload",
        "type": "schema",
        "schema": [{"name": "backup_password", "type": "string"}],
    },
]
BACKUP_OPTIONS = {
    "backup_password": BACKUP_PASSWORD,
    "days_between_backups": 3,
    "upload": {"backup_password": "nested-upload-password"},
}
BACKUP_INFO = {
    "result": {"slug": BACKUP_SLUG, "options": BACKUP_OPTIONS, "schema": BACKUP_SCHEMA}
}


def _settings(
    monkeypatch: pytest.MonkeyPatch, keys: str, *, redact: bool = True
) -> None:
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=redact, redact_app_option_keys=keys),
    )


def test_listed_key_is_redacted_in_every_app_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "backup_password")

    for slug in (BACKUP_SLUG, "core_samba"):
        out = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, slug)
        assert out["backup_password"] == REDACTED_SET
        assert out["upload"]["backup_password"] == REDACTED_SET
        assert out["days_between_backups"] == 3


def test_slug_scoped_key_leaves_other_apps_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, f"{BACKUP_SLUG}:backup_password")

    scoped = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, BACKUP_SLUG)
    other = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, "core_samba")

    assert scoped["backup_password"] == REDACTED_SET
    assert other == BACKUP_OPTIONS


def test_a_scope_without_the_repository_prefix_still_names_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "hassio_google_drive_backup:backup_password")

    out = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, BACKUP_SLUG)

    assert out["backup_password"] == REDACTED_SET


def test_listing_a_group_redacts_every_option_inside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "upload")

    out = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, BACKUP_SLUG)
    values = collect_addon_secret_values(BACKUP_OPTIONS, BACKUP_SCHEMA, BACKUP_SLUG)

    assert out["upload"] == {"backup_password": REDACTED_SET}
    assert out["backup_password"] == BACKUP_PASSWORD
    assert values == {"nested-upload-password"}


def test_a_listed_option_the_schema_dropped_is_still_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Supervisor keeps a saved value after an app update removes the option.
    _settings(monkeypatch, "old_password")
    options = {**BACKUP_OPTIONS, "old_password": "left-over-password"}

    out = redact_addon_options(options, BACKUP_SCHEMA, BACKUP_SLUG)
    values = collect_addon_secret_values(options, BACKUP_SCHEMA, BACKUP_SLUG)

    assert out["old_password"] == REDACTED_SET
    assert values == {"left-over-password"}


def test_a_listed_name_inside_a_group_the_schema_dropped_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "inner")
    options = {**BACKUP_OPTIONS, "old_group": {"inner": "inner-secret", "x": "1"}}

    out = redact_addon_options(options, BACKUP_SCHEMA, BACKUP_SLUG)
    values = collect_addon_secret_values(options, BACKUP_SCHEMA, BACKUP_SLUG)

    assert out["old_group"] == {"inner": REDACTED_SET, "x": "1"}
    assert values == {"inner-secret"}


def test_a_listed_group_covers_a_stored_child_its_schema_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "upload")
    options = {**BACKUP_OPTIONS, "upload": {"backup_password": "a-pw", "old": "b-pw"}}

    out = redact_addon_options(options, BACKUP_SCHEMA, BACKUP_SLUG)
    values = collect_addon_secret_values(options, BACKUP_SCHEMA, BACKUP_SLUG)

    assert out["upload"] == {"backup_password": REDACTED_SET, "old": REDACTED_SET}
    assert values == {"a-pw", "b-pw"}


def test_a_listed_group_the_schema_dropped_keeps_its_shape_and_feeds_the_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "old_group")
    options = {**BACKUP_OPTIONS, "old_group": {"inner": "inner-secret"}}

    out = redact_addon_options(options, BACKUP_SCHEMA, BACKUP_SLUG)
    values = collect_addon_secret_values(options, BACKUP_SCHEMA, BACKUP_SLUG)

    assert out["old_group"] == {"inner": REDACTED_SET}
    assert values == {"inner-secret"}


def test_a_listed_name_in_a_group_the_schema_turned_into_a_field_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A manual app update keeps the stored group under the new plain field;
    # a ``multiple`` group keeps its list of dicts.
    _settings(monkeypatch, "password")
    schema = [{"name": name, "type": "string"} for name in ("auth", "logins")]
    options = {
        "auth": {"password": "group-era-pw", "user": "u"},
        "logins": [{"password": "multi-era-pw", "user": "a"}],
    }

    out = redact_addon_options(options, schema, BACKUP_SLUG)
    values = collect_addon_secret_values(options, schema, BACKUP_SLUG)

    assert out == {
        "auth": {"password": REDACTED_SET, "user": "u"},
        "logins": [{"password": REDACTED_SET, "user": "a"}],
    }
    assert values == {"group-era-pw", "multi-era-pw"}


def test_a_listed_group_stored_as_a_plain_value_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "creds, tokens, unset")
    group = [{"name": "user", "type": "string"}]
    schema = [
        {"name": name, "type": "schema", "schema": group}
        for name in ("creds", "tokens", "unset")
    ]
    options = {"creds": "field-era-pw", "tokens": ["field-era-tok"], "unset": None}

    out = redact_addon_options(options, schema, BACKUP_SLUG)
    values = collect_addon_secret_values(options, schema, BACKUP_SLUG)

    assert out == {"creds": REDACTED_SET, "tokens": [REDACTED_SET], "unset": None}
    assert values == {"field-era-pw", "field-era-tok"}


def test_spacing_newlines_and_empty_entries_in_the_list_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, " , days_between_backups\n,, other_app: backup_password ,")

    out = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, BACKUP_SLUG)

    assert out["days_between_backups"] == REDACTED_SET
    assert out["backup_password"] == BACKUP_PASSWORD


def test_listed_values_feed_the_known_value_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "backup_password")

    values = collect_addon_secret_values(BACKUP_OPTIONS, BACKUP_SCHEMA, BACKUP_SLUG)

    assert values == {BACKUP_PASSWORD, "nested-upload-password"}


async def _get_info() -> dict:
    with patch(
        "ha_mcp.tools.tools_addons._supervisor_api_call",
        new_callable=AsyncMock,
        return_value=copy.deepcopy(BACKUP_INFO),
    ):
        return await get_addon_info(MagicMock(), BACKUP_SLUG)


@pytest.mark.asyncio
async def test_get_app_hides_a_slug_scoped_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, f"{BACKUP_SLUG}:backup_password")

    result = await _get_info()

    assert result["addon"]["options"]["backup_password"] == REDACTED_SET
    assert BACKUP_PASSWORD in known_secret_values()


@pytest.mark.asyncio
async def test_get_app_warns_when_a_name_scoped_to_it_matches_no_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, f"{BACKUP_SLUG}:backup_pasword, {BACKUP_SLUG}:upload")

    result = await _get_info()

    assert len(result["warnings"]) == 1
    assert "backup_pasword" in result["warnings"][0]
    assert "upload" not in result["warnings"][0]


@pytest.mark.asyncio
async def test_get_app_does_not_warn_about_a_listed_option_the_schema_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, f"{BACKUP_SLUG}:old_password")
    info = copy.deepcopy(BACKUP_INFO)
    info["result"]["options"]["old_password"] = "left-over-password"

    with patch(
        "ha_mcp.tools.tools_addons._supervisor_api_call",
        new_callable=AsyncMock,
        return_value=info,
    ):
        result = await get_addon_info(MagicMock(), BACKUP_SLUG)

    assert result["addon"]["options"]["old_password"] == REDACTED_SET
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_get_app_without_a_schema_still_registers_listed_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "backup_password")
    info = copy.deepcopy(BACKUP_INFO)
    info["result"]["schema"] = []

    with patch(
        "ha_mcp.tools.tools_addons._supervisor_api_call",
        new_callable=AsyncMock,
        return_value=info,
    ):
        result = await get_addon_info(MagicMock(), BACKUP_SLUG)

    assert result["addon"]["options"]["backup_password"] == REDACTED_SET
    assert BACKUP_PASSWORD in known_secret_values()
    assert "nested-upload-password" in known_secret_values()


@pytest.mark.asyncio
async def test_list_is_ignored_while_redact_secrets_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, "backup_password", redact=False)

    result = await _get_info()

    assert result["addon"]["options"] == BACKUP_OPTIONS
    assert known_secret_values() == ()


@pytest.mark.asyncio
async def test_password_written_to_a_listed_key_is_registered_for_the_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, f"{BACKUP_SLUG}:backup_password")
    responses = [copy.deepcopy(BACKUP_INFO), {"success": True, "result": {}}]

    with patch(
        "ha_mcp.tools.tools_addons._supervisor_api_call",
        new_callable=AsyncMock,
        side_effect=responses,
    ):
        await AddOnTools(MagicMock())._execute_config_mode(
            BACKUP_SLUG, {"options": {"backup_password": "a-brand-new-password"}}
        )

    assert "a-brand-new-password" in known_secret_values()
    assert BACKUP_PASSWORD in known_secret_values()
