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

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


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
            result = await mcp_client.call_tool("ha_backup_create", {})

            data = parse_mcp_result(result)
            logger.info(f"📦 Backup creation result: {data}")

            # Check if backup password is configured
            if not data.get("success"):
                error = data.get("error", "")
                if "password" in error.lower():
                    logger.warning("⚠️ Test environment doesn't have default backup password configured")
                    pytest.skip("Test environment missing default backup password")
                else:
                    raise AssertionError(f"Backup creation failed: {error}")

            # Verify backup was created successfully
            assert "backup_job_id" in data, "No backup_job_id returned"
            assert "name" in data, "No backup name returned"
            assert data["name"].startswith("MCP_Backup_"), f"Unexpected backup name: {data['name']}"

            backup_job_id = data["backup_job_id"]
            backup_name = data["name"]
            backup_id = data.get("backup_id")

            logger.info(f"✅ Backup created: {backup_name} (ID: {backup_id}, job: {backup_job_id})")

            # Verify backup completed (tool waits for completion)
            assert "status" in data, "No status returned"
            assert "completed" in data["status"].lower(), f"Backup did not complete: {data['status']}"

            # Log backup details
            if "duration_seconds" in data:
                logger.info(f"⏱️ Backup duration: {data['duration_seconds']} seconds")
            if "size_bytes" in data:
                size_mb = data["size_bytes"] / (1024 * 1024)
                logger.info(f"📦 Backup size: {size_mb:.2f} MB")

            logger.info(f"✅ Backup test completed successfully")

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

            result = await mcp_client.call_tool(
                "ha_backup_create", {"name": custom_name}
            )

            data = parse_mcp_result(result)
            logger.info(f"📦 Backup creation result: {data}")

            # Check if backup password is configured
            if not data.get("success"):
                error = data.get("error", "")
                if "password" in error.lower():
                    logger.warning("⚠️ Test environment doesn't have default backup password configured")
                    pytest.skip("Test environment missing default backup password")
                else:
                    raise AssertionError(f"Backup creation failed: {error}")

            # Verify backup was created successfully
            assert "backup_job_id" in data, "No backup_job_id returned"
            assert "backup_id" in data, "No backup_id returned"
            assert data["name"] == custom_name, f"Backup name mismatch: {data['name']} != {custom_name}"

            backup_job_id = data["backup_job_id"]
            backup_id = data["backup_id"]

            logger.info(f"✅ Backup created: {custom_name} (ID: {backup_id}, job: {backup_job_id})")

            # Verify backup completed (tool waits for completion)
            assert "completed" in data["status"].lower(), f"Backup did not complete: {data['status']}"

            logger.info(f"✅ Custom name backup test completed successfully")

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
            result = await mcp_client.call_tool(
                "ha_backup_restore",
                {"backup_id": "nonexistent_backup_id_12345"},
            )

            data = parse_mcp_result(result)
            logger.info(f"📊 Restore validation result: {data}")

            # Should fail with helpful error
            assert data.get("success") is False, "Expected restore to fail for non-existent backup"
            assert "not found" in data.get("error", "").lower(), f"Expected 'not found' error, got: {data.get('error')}"
            assert "available_backups" in data, "Should provide list of available backups"

            available_backups = data.get("available_backups", [])
            logger.info(f"📋 Available backups: {len(available_backups)} found")

            if available_backups:
                # Log available backup info
                for backup in available_backups[:3]:
                    logger.info(
                        f"  - {backup.get('name')} (ID: {backup.get('backup_id')}, Date: {backup.get('date')})"
                    )

                logger.info("✅ Restore validation provides helpful feedback")

            logger.info("✅ Backup restore validation test completed successfully")

        except Exception as e:
            logger.error(f"❌ Backup restore validation test failed: {e}")
            raise

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
            result = await mcp_client.call_tool("ha_backup_create", {})

            data = parse_mcp_result(result)
            logger.info(f"📦 Backup result: {data}")

            # If backup succeeded, config was retrieved successfully
            if data.get("success"):
                logger.info("✅ Backup config and password retrieved successfully")
                assert "note" in data, "Should include encryption note"
                assert "password" in data["note"].lower(), "Should mention password in note"
            else:
                # Check if error is about missing password
                error = data.get("error", "")
                if "password" in error.lower():
                    logger.warning(
                        "⚠️ Test environment doesn't have default backup password configured"
                    )
                    pytest.skip("Test environment missing default backup password")
                else:
                    raise AssertionError(f"Unexpected backup creation error: {error}")

            logger.info("✅ Password retrieval test completed successfully")

        except Exception as e:
            logger.error(f"❌ Password retrieval test failed: {e}")
            raise
