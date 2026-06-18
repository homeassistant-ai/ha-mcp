"""End-to-End tests for pre-#1579 legacy backup access via ha_manage_backup.

Pre-#1579 the YAML tool wrote whole-file ``.bak`` copies into
``<config>/.ha_mcp_tools_backups/``. #1579 hard-swapped to the shared edits
store but those historical artifacts must stay restorable *through the tool*.
This exercises the full round-trip (`list` / `view` / `diff` / `restore`) over
the legacy store against real artifacts staged by the fixture.

The two ``.bak`` files are seeded **pre-boot** by ``ha_container_with_fresh_config``
(see ``_seed_legacy_yaml_backups`` in conftest) — a post-boot host write to the
bind-mounted config dir doesn't propagate in CI, so the round-trip reads
fixture-staged files that exist when the component boots. The toggle-off
mandatory-write refusal is covered separately by
``TestMandatoryBackupRefusal`` in ``test_file_operations.py``.
"""

import logging

from ...utilities.assertions import extract_error_message, safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Fixed artifacts staged pre-boot by conftest._seed_legacy_yaml_backups.
_UNAMBIGUOUS = "legacy:themes_e2elegacy.yaml.20200101_000000.bak"
_AMBIGUOUS = "legacy:packages_foo_bar.yaml.20200101_000000.bak"


def _legacy_entry(listed: dict, name: str) -> dict:
    """Return the single legacy list entry named ``name`` (asserts exactly one)."""
    entries = listed["data"]["backups"]
    matches = [e for e in entries if e.get("name") == name]
    assert len(matches) == 1, entries
    return matches[0]


class TestLegacyBackupAccess:
    """Legacy .ha_mcp_tools_backups/ surfaced through ha_manage_backup edits."""

    async def test_list_view_diff_restore_unambiguous(self, mcp_client_with_filesystem):
        mcp = mcp_client_with_filesystem

        # LIST — the seeded legacy entry surfaces with source + decoded path.
        listed = await safe_call_tool(
            mcp, "ha_manage_backup", {"scope": "edits", "action": "list"}
        )
        assert listed.get("success") is True, listed
        entry = _legacy_entry(listed, _UNAMBIGUOUS)
        assert entry["source"] == "legacy", entry
        assert entry["domain"] == "yaml_file", entry
        assert entry["entity_id"] == "themes/e2elegacy.yaml", entry
        assert entry["path_ambiguous"] is False, entry

        # VIEW — raw .bak content.
        viewed = await safe_call_tool(
            mcp,
            "ha_manage_backup",
            {"scope": "edits", "action": "view", "backup_name": _UNAMBIGUOUS},
        )
        assert viewed.get("success") is True, viewed
        assert "e2elegacy" in viewed["data"]["content"], viewed

        # DIFF — text diff (file may or may not exist; kind is always text).
        diff = await safe_call_tool(
            mcp,
            "ha_manage_backup",
            {"scope": "edits", "action": "diff", "backup_name": _UNAMBIGUOUS},
        )
        assert diff.get("success") is True, diff
        assert diff["data"]["kind"] == "text", diff

        # RESTORE — writes themes/e2elegacy.yaml via edit_yaml_config(replace_file).
        restored = await safe_call_tool(
            mcp,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": _UNAMBIGUOUS},
        )
        assert restored.get("success") is True, restored
        assert restored["data"]["domain"] == "yaml_file", restored
        assert restored["data"]["entity_id"] == "themes/e2elegacy.yaml", restored

        # The file now exists on disk with the restored content.
        read = await safe_call_tool(
            mcp, "ha_read_file", {"path": "themes/e2elegacy.yaml"}
        )
        assert read.get("success") is True, read
        assert "e2elegacy" in read["content"], read["content"]

    async def test_ambiguous_name_restore_refused_but_view_works(
        self, mcp_client_with_filesystem
    ):
        mcp = mcp_client_with_filesystem

        listed = await safe_call_tool(
            mcp, "ha_manage_backup", {"scope": "edits", "action": "list"}
        )
        assert listed.get("success") is True, listed
        entry = _legacy_entry(listed, _AMBIGUOUS)
        assert entry["path_ambiguous"] is True, entry

        # Restore refuses an ambiguous target rather than guessing.
        refused = await safe_call_tool(
            mcp,
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": _AMBIGUOUS},
        )
        assert refused.get("success") is not True, refused
        msg = extract_error_message(refused).lower()
        assert "unambiguously" in msg or "manually" in msg, refused

        # View still works — the content is always readable.
        viewed = await safe_call_tool(
            mcp,
            "ha_manage_backup",
            {"scope": "edits", "action": "view", "backup_name": _AMBIGUOUS},
        )
        assert viewed.get("success") is True, viewed
        assert "switch" in viewed["data"]["content"], viewed
