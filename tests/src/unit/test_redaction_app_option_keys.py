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


def _settings(monkeypatch, keys: str, *, redact: bool = True) -> None:
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=redact, redact_app_option_keys=keys),
    )


def test_listed_key_is_redacted_in_every_app_and_nothing_else(monkeypatch):
    _settings(monkeypatch, "backup_password")

    for slug in (BACKUP_SLUG, "core_samba"):
        out = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, slug)
        assert out["backup_password"] == REDACTED_SET
        assert out["upload"]["backup_password"] == REDACTED_SET
        assert out["days_between_backups"] == 3


def test_slug_scoped_key_leaves_other_apps_untouched(monkeypatch):
    _settings(monkeypatch, f"{BACKUP_SLUG}:backup_password")

    scoped = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, BACKUP_SLUG)
    other = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, "core_samba")

    assert scoped["backup_password"] == REDACTED_SET
    assert other == BACKUP_OPTIONS


def test_spacing_newlines_and_empty_entries_in_the_list_are_ignored(monkeypatch):
    _settings(monkeypatch, " , days_between_backups\n,, other_app: backup_password ,")

    out = redact_addon_options(BACKUP_OPTIONS, BACKUP_SCHEMA, BACKUP_SLUG)

    assert out["days_between_backups"] == REDACTED_SET
    assert out["backup_password"] == BACKUP_PASSWORD


def test_listed_values_feed_the_known_value_scrub(monkeypatch):
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
async def test_get_app_hides_a_slug_scoped_password(monkeypatch):
    _settings(monkeypatch, f"{BACKUP_SLUG}:backup_password")

    result = await _get_info()

    assert result["addon"]["options"]["backup_password"] == REDACTED_SET
    assert BACKUP_PASSWORD in known_secret_values()


@pytest.mark.asyncio
async def test_list_is_ignored_while_redact_secrets_is_off(monkeypatch):
    _settings(monkeypatch, "backup_password", redact=False)

    result = await _get_info()

    assert result["addon"]["options"] == BACKUP_OPTIONS
    assert known_secret_values() == ()


@pytest.mark.asyncio
async def test_password_written_to_a_listed_key_is_registered_for_the_scrub(
    monkeypatch,
):
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
