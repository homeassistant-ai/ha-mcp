"""
Backup Tools E2E Tests

NOTE: Run these tests with the Docker test environment:
    export HAMCP_ENV_FILE=tests/.env.test && uv run pytest tests/src/e2e/workflows/convenience/test_backup.py -v

Or ensure Docker test environment is running:
    cd tests && docker compose up -d

Tests for backup MCP tools that provide safety mechanisms:
- Backup creation (fast, local, encrypted)
- Backup restoration (with safety mechanisms)

These tools are critical for configuration safety and disaster recovery.
"""

import asyncio
import logging
import time

import pytest

from ...utilities.assertions import safe_call_tool

logger = logging.getLogger(__name__)


def _error_message(data: dict) -> str:
    error = data.get("error", {})
    return error.get("message", str(error)) if isinstance(error, dict) else str(error)


@pytest.mark.convenience
class TestBackupTools:
    """Test backup tools for configuration safety."""

    async def test_backup_create_with_auto_name(self, mcp_client):
        """
        Test: Create backup with auto-generated name

        This test validates that backups can be created quickly without
        specifying a name, using automatic naming.
        """

        logger.info("💾 Testing backup creation with auto-generated name...")

        try:
            # Create backup without name (auto-generated)
            logger.info("📦 Creating backup (auto-named)...")
            data = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "snapshot", "action": "create"},
            )

            logger.info(f"📦 Backup creation result: {data}")

            # Check if backup password is configured
            if not data.get("success"):
                error = data.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                if "password" in error_msg.lower():
                    logger.warning(
                        "⚠️ Test environment doesn't have default backup password configured"
                    )
                    pytest.skip("Test environment missing default backup password")
                else:
                    raise AssertionError(f"Backup creation failed: {error_msg}")

            # Verify backup was created successfully
            assert "backup_job_id" in data, "No backup_job_id returned"
            assert "name" in data, "No backup name returned"
            assert data["name"].startswith("MCP_Backup_"), (
                f"Unexpected backup name: {data['name']}"
            )

            backup_job_id = data["backup_job_id"]
            backup_name = data["name"]
            backup_id = data.get("backup_id")

            logger.info(
                f"✅ Backup created: {backup_name} (ID: {backup_id}, job: {backup_job_id})"
            )

            # Verify backup completed (tool waits for completion)
            assert "status" in data, "No status returned"
            assert "completed" in data["status"].lower(), (
                f"Backup did not complete: {data['status']}"
            )

            # Log backup details
            if "duration_seconds" in data:
                logger.info(f"⏱️ Backup duration: {data['duration_seconds']} seconds")
            if "size_bytes" in data:
                size_mb = data["size_bytes"] / (1024 * 1024)
                logger.info(f"📦 Backup size: {size_mb:.2f} MB")

            logger.info("✅ Backup test completed successfully")

        except Exception as e:
            logger.error(f"❌ Backup creation test failed: {e}")
            raise

    async def test_backup_create_with_custom_name(self, mcp_client):
        """
        Test: Create backup with custom name

        This test validates that backups can be created with user-specified names.
        """

        logger.info("💾 Testing backup creation with custom name...")

        try:
            # Create backup with custom name
            custom_name = f"E2E_Test_Backup_{int(time.time())}"
            logger.info(f"📦 Creating backup: {custom_name}...")

            data = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "snapshot", "action": "create", "name": custom_name},
            )

            logger.info(f"📦 Backup creation result: {data}")

            # Check if backup password is configured
            if not data.get("success"):
                error = data.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                if "password" in error_msg.lower():
                    logger.warning(
                        "⚠️ Test environment doesn't have default backup password configured"
                    )
                    pytest.skip("Test environment missing default backup password")
                else:
                    raise AssertionError(f"Backup creation failed: {error_msg}")

            # Verify backup was created successfully
            assert "backup_job_id" in data, "No backup_job_id returned"
            assert "backup_id" in data, "No backup_id returned"
            assert data["name"] == custom_name, (
                f"Backup name mismatch: {data['name']} != {custom_name}"
            )

            backup_job_id = data["backup_job_id"]
            backup_id = data["backup_id"]

            logger.info(
                f"✅ Backup created: {custom_name} (ID: {backup_id}, job: {backup_job_id})"
            )

            # Verify backup completed (tool waits for completion)
            assert "completed" in data["status"].lower(), (
                f"Backup did not complete: {data['status']}"
            )

            logger.info("✅ Custom name backup test completed successfully")

        except Exception as e:
            logger.error(f"❌ Custom name backup creation test failed: {e}")
            raise

    @pytest.mark.slow
    async def test_backup_restore_validation(self, mcp_client):
        """
        Test: Backup restore validation (without actually restoring)

        This test validates that restore properly checks for backup existence
        and provides helpful error messages, WITHOUT actually performing a restore.

        Marked as slow because we test the safety backup creation flow.

        TODO: Actual restore testing would be valuable but tricky to implement.
        Would need to verify system state before/after restore, handle HA restart,
        and ensure test environment can recover. See GitHub wiki tech debt section.
        """

        logger.info("🔄 Testing backup restore validation...")

        try:
            # Test 1: Try to restore non-existent backup
            logger.info("🔍 Testing restore with non-existent backup ID...")
            data = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {
                    "scope": "snapshot",
                    "action": "restore",
                    "backup_id": "nonexistent_backup_id_12345",
                },
            )

            logger.info(f"📊 Restore validation result: {data}")

            # Should fail with helpful error
            assert data.get("success") is False, (
                "Expected restore to fail for non-existent backup"
            )
            error = data.get("error", {})
            error_msg = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            assert "not found" in error_msg.lower(), (
                f"Expected 'not found' error, got: {error_msg}"
            )
            # Verify helpful guidance is provided (either in suggestion or as a key)
            suggestion = error.get("suggestion", "") if isinstance(error, dict) else ""
            assert suggestion or "available_backups" in data, (
                "Should provide guidance on available backups"
            )
            logger.info("✅ Restore validation provides helpful feedback")

            logger.info("✅ Backup restore validation test completed successfully")

        except Exception as e:
            logger.error(f"❌ Backup restore validation test failed: {e}")
            raise

    async def test_snapshot_list(self, mcp_client):
        """
        Test: List full HA snapshot tarballs (scope='snapshot', action='list').

        Read-only inventory via HA's WebSocket ``backup/info`` (issue #1586) —
        lets a caller discover backup IDs / confirm a backup landed without
        already knowing an ID. No restart, no mutation.
        """

        logger.info("📋 Testing snapshot list...")

        data = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "snapshot", "action": "list"},
        )

        logger.info(f"📋 Snapshot list result: {data}")

        assert data.get("success") is True, f"Snapshot list failed: {data}"
        assert "backups" in data, "No 'backups' key in list result"
        assert isinstance(data["backups"], list), "'backups' should be a list"
        assert "count" in data and "total" in data, "Missing count/total keys"
        # Each entry exposes the discovery fields a caller needs.
        for entry in data["backups"]:
            assert "backup_id" in entry
            assert "date" in entry

        logger.info(f"✅ Snapshot list returned {data['count']} backup(s)")

    async def test_backup_config_password_retrieval(self, mcp_client):
        """
        Test: Verify backup configuration and password retrieval

        This test ensures the backup tools can retrieve the default backup
        password from Home Assistant configuration.
        """

        logger.info("🔑 Testing backup configuration password retrieval...")

        try:
            # Create a backup (which internally retrieves config/password)
            logger.info("📦 Creating backup to test config retrieval...")
            data = await safe_call_tool(
                mcp_client,
                "ha_manage_backup",
                {"scope": "snapshot", "action": "create"},
            )

            logger.info(f"📦 Backup result: {data}")

            # If backup succeeded, config was retrieved successfully
            if data.get("success"):
                logger.info("✅ Backup config and password retrieved successfully")
                assert "note" in data, "Should include encryption note"
                assert "password" in data["note"].lower(), (
                    "Should mention password in note"
                )
            else:
                # Check if error is about missing password
                error = data.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                if "password" in error_msg.lower():
                    logger.warning(
                        "⚠️ Test environment doesn't have default backup password configured"
                    )
                    pytest.skip("Test environment missing default backup password")
                else:
                    raise AssertionError(
                        f"Unexpected backup creation error: {error_msg}"
                    )

            logger.info("✅ Password retrieval test completed successfully")

        except Exception as e:
            logger.error(f"❌ Password retrieval test failed: {e}")
            raise


@pytest.mark.convenience
class TestSnapshotDelete:
    """E2E coverage for scope='snapshot', action='delete' (#1861).

    ``ENABLE_SNAPSHOT_DELETE=true`` is set for the whole e2e suite (see
    conftest.py) so these guard paths are reachable; production defaults
    the feature off. Guard-rejection tests don't need an actual backup on
    disk, since most guards run before or independent of state; the success
    path lowers ``snapshot_delete_min_age_days`` to 0 for the duration of
    the test so a just-created backup clears the age floor.
    """

    async def test_delete_requires_confirm(self, mcp_client):
        logger.info("🗑️ Testing snapshot delete without confirm...")

        data = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "snapshot", "action": "delete", "backup_id": "does-not-matter"},
        )

        assert data.get("success") is False, "Expected delete without confirm to fail"
        assert "confirm" in _error_message(data).lower()

    async def test_delete_nonexistent_backup_id(self, mcp_client):
        logger.info("🗑️ Testing snapshot delete of a nonexistent backup id...")

        data = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "delete",
                "backup_id": "nonexistent_backup_id_12345",
                "confirm": True,
            },
        )

        assert data.get("success") is False, (
            "Expected delete to fail for a nonexistent backup"
        )
        assert "not found" in _error_message(data).lower()

    async def test_delete_refuses_freshly_created_backup(self, mcp_client):
        """The default 7-day age floor blocks deleting a backup created
        moments ago — proves the guard is live end-to-end, not just at the
        unit level.

        A second, newer backup is created after the target so the target
        is NOT also the single newest snapshot — otherwise the newest-
        snapshot guard (checked first; see backup.py) would fire instead
        of the age-floor guard this test is meant to isolate, since a
        freshly created backup with nothing after it is trivially "newest"
        in a shared test container.
        """
        logger.info("🗑️ Testing snapshot delete refuses a too-young backup...")

        created = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "create",
                "name": f"E2E_Delete_TooYoung_{int(time.time())}",
            },
        )
        if not created.get("success"):
            if "password" in _error_message(created).lower():
                pytest.skip("Test environment missing default backup password")
            raise AssertionError(f"Backup creation failed: {_error_message(created)}")
        backup_id = created["backup_id"]

        await asyncio.sleep(1.1)

        newer = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "create",
                "name": f"E2E_Delete_TooYoung_Newer_{int(time.time())}",
            },
        )
        assert newer.get("success") is True, (
            f"Second backup creation failed: {_error_message(newer)}"
        )

        data = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "delete",
                "backup_id": backup_id,
                "confirm": True,
            },
        )

        assert data.get("success") is False, (
            "Expected delete to be refused for a freshly created backup"
        )
        assert "days" in _error_message(data).lower()

    async def test_delete_succeeds_with_age_floor_disabled(
        self, mcp_client, monkeypatch
    ):
        """Full happy path: with the age floor disabled, an old-enough
        (non-newest) ad-hoc backup can actually be deleted end-to-end."""
        logger.info("🗑️ Testing snapshot delete success path...")

        from ha_mcp.config import _reset_global_settings

        monkeypatch.setenv("SNAPSHOT_DELETE_MIN_AGE_DAYS", "0")
        _reset_global_settings()

        # Two backups so the target is never the single newest snapshot.
        target = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "create",
                "name": f"E2E_Delete_Target_{int(time.time())}",
            },
        )
        if not target.get("success"):
            if "password" in _error_message(target).lower():
                pytest.skip("Test environment missing default backup password")
            raise AssertionError(f"Backup creation failed: {_error_message(target)}")

        # Guarantee a distinct, strictly-later `date` for the second backup —
        # `_newest_backup_id` picks the newest by date, and two creates close
        # enough together could otherwise tie under coarse timestamp
        # precision, making `target` spuriously look like the newest.
        await asyncio.sleep(1.1)

        newer = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "create",
                "name": f"E2E_Delete_Newer_{int(time.time())}",
            },
        )
        assert newer.get("success") is True, (
            f"Second backup creation failed: {_error_message(newer)}"
        )

        backup_id = target["backup_id"]
        data = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {
                "scope": "snapshot",
                "action": "delete",
                "backup_id": backup_id,
                "confirm": True,
            },
        )

        assert data.get("success") is True, f"Snapshot delete failed: {data}"
        assert data["backup_id"] == backup_id

        # Verify it's actually gone via the list action.
        listing = await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "snapshot", "action": "list"},
        )
        remaining_ids = {b["backup_id"] for b in listing["backups"]}
        assert backup_id not in remaining_ids, (
            "Deleted backup_id still present in snapshot list"
        )

        logger.info("✅ Snapshot delete success path completed")
