"""End-to-End tests for pre-#1579 legacy backup access via ha_manage_backup.

Pre-#1579 the YAML tool wrote whole-file ``.bak`` copies into
``<config>/.ha_mcp_tools_backups/``. #1579 hard-swapped to the shared edits
store but those historical artifacts must stay restorable *through the tool*
(maintainer requirement). These tests seed a legacy ``.bak`` directly on disk
(the old write path is gone) and exercise list / view / diff / restore over it,
plus the ambiguous-name restore refusal.

Container-mode only: the tests write a raw file into the host config dir mounted
at ``/config``; HAOS/inaddon mode has no host ``config_path`` so the seed can't
be placed (``ha_container_with_fresh_config["config_path"]`` is ``None``).
"""

import logging
import uuid
from pathlib import Path

import pytest

from ...utilities.assertions import extract_error_message, safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _require_config_path(container_info: dict) -> Path:
    """Host config dir mounted at ``/config`` (skips in HAOS/inaddon mode)."""
    cfg = container_info.get("config_path")
    if cfg is None:
        pytest.skip("requires host config_path access (container mode only)")
    assert cfg is not None  # narrow for type-checkers (skip is NoReturn)
    return Path(cfg)


def _seed_bak(legacy_dir: Path, safe_name: str, content: str) -> str:
    """Write a legacy ``<safe>.<ts>.bak`` and return its ``legacy:`` backup id."""
    legacy_dir.mkdir(parents=True, exist_ok=True)
    bak_name = f"{safe_name}.20200101_000000.bak"
    (legacy_dir / bak_name).write_text(content)
    return bak_name


class TestLegacyBackupAccess:
    """Legacy .ha_mcp_tools_backups/ surfaced through ha_manage_backup edits."""

    async def test_list_view_diff_restore_unambiguous(
        self, ha_container_with_fresh_config, mcp_client
    ):
        config_path = _require_config_path(ha_container_with_fresh_config)
        legacy_dir = config_path / ".ha_mcp_tools_backups"
        marker = uuid.uuid4().hex[:8]
        # No underscore in the basename → the path decode is unambiguous.
        rel = f"themes/e2elegacy{marker}.yaml"
        safe = rel.replace("/", "_")
        content = f"e2elegacy{marker}:\n  primary-color: '#abcdef'\n"
        bak_name = _seed_bak(legacy_dir, safe, content)
        legacy_id = f"legacy:{bak_name}"
        restored_file = config_path / rel

        try:
            # LIST — the legacy entry surfaces with source + decoded path.
            listed = await safe_call_tool(
                mcp_client, "ha_manage_backup", {"scope": "edits", "action": "list"}
            )
            assert listed.get("success") is True, listed
            entries = listed["data"]["backups"]
            mine = [e for e in entries if e.get("name") == legacy_id]
            assert len(mine) == 1, entries
            assert mine[0]["source"] == "legacy", mine[0]
            assert mine[0]["domain"] == "yaml_file", mine[0]
            assert mine[0]["entity_id"] == rel, mine[0]
            assert mine[0]["path_ambiguous"] is False, mine[0]

            # VIEW — raw .bak content.
            viewed = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "view", "backup_name": legacy_id},
            )
            assert viewed.get("success") is True, viewed
            assert viewed["data"]["content"] == content, viewed

            # DIFF — text diff; file doesn't exist yet → entity_missing.
            diff = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "diff", "backup_name": legacy_id},
            )
            assert diff.get("success") is True, diff
            assert diff["data"]["kind"] == "text", diff

            # RESTORE — writes the file via edit_yaml_config(replace_file).
            restored = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": legacy_id},
            )
            assert restored.get("success") is True, restored
            assert restored["data"]["domain"] == "yaml_file", restored
            assert restored["data"]["entity_id"] == rel, restored

            # The file now exists on disk with the restored content.
            read = await safe_call_tool(mcp_client, "ha_read_file", {"path": rel})
            assert read.get("success") is True, read
            assert f"e2elegacy{marker}" in read["content"], read["content"]
        finally:
            # Clean up host-side: ha_delete_file is mandatory-backup-gated and
            # would refuse with auto-backup off, so unlink directly.
            restored_file.unlink(missing_ok=True)
            (legacy_dir / bak_name).unlink(missing_ok=True)

    async def test_ambiguous_name_restore_refused_but_view_works(
        self, ha_container_with_fresh_config, mcp_client
    ):
        legacy_dir = (
            _require_config_path(ha_container_with_fresh_config)
            / ".ha_mcp_tools_backups"
        )
        marker = uuid.uuid4().hex[:8]
        # A literal underscore in the basename makes the path decode ambiguous
        # (can't tell packages/foo_bar.yaml from packages/foo/bar.yaml).
        safe = f"packages_foo_bar{marker}.yaml"
        content = f"# legacy {marker}\nswitch: []\n"
        bak_name = _seed_bak(legacy_dir, safe, content)
        legacy_id = f"legacy:{bak_name}"

        try:
            listed = await safe_call_tool(
                mcp_client, "ha_manage_backup", {"scope": "edits", "action": "list"}
            )
            assert listed.get("success") is True, listed
            mine = [e for e in listed["data"]["backups"] if e.get("name") == legacy_id]
            assert len(mine) == 1, listed["data"]["backups"]
            assert mine[0]["path_ambiguous"] is True, mine[0]

            # Restore refuses an ambiguous target rather than guessing.
            refused = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": legacy_id},
            )
            assert refused.get("success") is not True, refused
            msg = extract_error_message(refused).lower()
            assert "unambiguously" in msg or "manually" in msg, refused

            # View still works — the content is always readable.
            viewed = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "edits", "action": "view", "backup_name": legacy_id},
            )
            assert viewed.get("success") is True, viewed
            assert viewed["data"]["content"] == content, viewed
        finally:
            (legacy_dir / bak_name).unlink(missing_ok=True)
