"""
Backup and restore tools for Home Assistant MCP Server.

Provides backup creation and restoration capabilities with safety mechanisms.
"""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Annotated

from pydantic import Field

from ..client.rest_client import HomeAssistantClient
from ..client.websocket_client import HomeAssistantWebSocketClient
from .helpers import get_backup_hint_text, get_connected_ws_client

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


async def _get_backup_password(
    ws_client: HomeAssistantWebSocketClient,
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Retrieve default backup password from Home Assistant configuration.

    Args:
        ws_client: Connected WebSocket client

    Returns:
        Tuple of (password, error_dict). If retrieval fails, password is None.
    """
    backup_config = await ws_client.send_command("backup/config/info")
    if not backup_config.get("success"):
        return None, {
            "success": False,
            "error": "Failed to retrieve backup configuration",
            "details": backup_config,
        }

    config_data = backup_config.get("result", {}).get("config", {})
    default_password = config_data.get("create_backup", {}).get("password")

    if not default_password:
        return None, {
            "success": False,
            "error": "No default backup password configured in Home Assistant",
            "suggestion": "Configure automatic backups in Home Assistant settings to set a default password",
        }

    return default_password, None


async def create_backup(
    client: HomeAssistantClient, name: str | None = None
) -> dict[str, Any]:
    """
    Create a fast Home Assistant backup (local only, excludes database).

    Args:
        client: Home Assistant REST client
        name: Optional backup name (auto-generated if not provided)

    Returns:
        Dictionary with backup result including backup_id, status, duration, etc.
    """
    ws_client = None

    try:
        # Connect to WebSocket
        ws_client, error = await get_connected_ws_client(client.base_url, client.token)
        if error:
            return error

        # Get backup password
        password, error = await _get_backup_password(ws_client)
        if error:
            return error

        # Generate backup name if not provided
        if not name:
            now = datetime.now()
            name = f"MCP_Backup_{now.strftime('%Y-%m-%d_%H:%M:%S')}"

        # Create backup request
        backup_params = {
            "name": name,
            "password": password,
            "agent_ids": ["hassio.local"],  # Local only
            "include_homeassistant": True,
            "include_database": False,  # Fast backup
            "include_all_addons": True,
        }

        # Send backup request
        result = await ws_client.send_command("backup/generate", **backup_params)

        if not result.get("success"):
            return {
                "success": False,
                "error": "Backup creation failed",
                "details": result,
            }

        backup_job_id = result.get("result", {}).get("backup_job_id")
        logger.info(f"Backup job started: {backup_job_id}, waiting for completion...")

        # Wait for backup to complete by polling backup/info
        max_wait_seconds = 120  # 2 minutes max wait
        poll_interval = 2  # Check every 2 seconds
        waited = 0

        while waited < max_wait_seconds:
            await asyncio.sleep(poll_interval)
            waited += poll_interval

            # Check backup status
            info_result = await ws_client.send_command("backup/info")
            if info_result.get("success"):
                state = info_result.get("result", {}).get("state")
                last_event = info_result.get("result", {}).get("last_action_event", {})
                event_state = last_event.get("state")

                logger.debug(
                    f"Backup state: {state}, event_state: {event_state}, waited: {waited}s"
                )

                # Check if backup is complete
                if state == "idle" and event_state == "completed":
                    # Find the backup that was just created
                    backups = info_result.get("result", {}).get("backups", [])
                    created_backup = None
                    for backup in backups:
                        if backup.get("name") == name:
                            created_backup = backup
                            break

                    if created_backup:
                        logger.info(
                            f"Backup completed successfully: {created_backup.get('backup_id')}"
                        )
                        return {
                            "success": True,
                            "backup_id": created_backup.get("backup_id"),
                            "backup_job_id": backup_job_id,
                            "name": name,
                            "date": created_backup.get("date"),
                            "size_bytes": created_backup.get("agents", {})
                            .get("hassio.local", {})
                            .get("size"),
                            "status": "Backup completed successfully",
                            "duration_seconds": waited,
                            "note": "Backup uses your Home Assistant's default backup password",
                        }
                    else:
                        # Backup completed but not found in list yet
                        logger.warning(
                            "Backup completed but not found in backup list yet, waiting..."
                        )
                        continue

                # Check if backup failed
                elif event_state == "failed":
                    return {
                        "success": False,
                        "error": "Backup creation failed",
                        "backup_job_id": backup_job_id,
                        "last_event": last_event,
                    }

        # Timeout waiting for backup
        logger.warning(f"Backup did not complete within {max_wait_seconds} seconds")
        return {
            "success": False,
            "error": f"Backup creation timed out after {max_wait_seconds} seconds",
            "backup_job_id": backup_job_id,
            "name": name,
            "suggestion": "Backup may still be in progress. Check Home Assistant backup status.",
        }

    except Exception as e:
        logger.error(f"Error creating backup: {e}")
        return {
            "success": False,
            "error": f"Failed to create backup: {str(e)}",
            "suggestion": "Check Home Assistant connection and backup configuration",
        }
    finally:
        # Always disconnect WebSocket
        if ws_client:
            try:
                await ws_client.disconnect()
            except:
                pass  # Ignore errors during cleanup


async def restore_backup(
    client: HomeAssistantClient, backup_id: str, restore_database: bool = False
) -> dict[str, Any]:
    """
    Restore Home Assistant from a backup (DESTRUCTIVE - use with caution).

    Creates a safety backup before restore to allow rollback if needed.

    Args:
        client: Home Assistant REST client
        backup_id: Backup ID to restore
        restore_database: Whether to restore database (historical data)

    Returns:
        Dictionary with restore result including safety_backup_id, status, etc.
    """
    ws_client = None

    try:
        # Connect to WebSocket
        ws_client, error = await get_connected_ws_client(client.base_url, client.token)
        if error:
            return error

        # Verify backup exists
        backup_info = await ws_client.send_command("backup/info")
        if not backup_info.get("success"):
            return {
                "success": False,
                "error": "Failed to retrieve backup information",
                "details": backup_info,
            }

        backups = backup_info.get("result", {}).get("backups", [])
        backup_exists = any(b.get("backup_id") == backup_id for b in backups)

        if not backup_exists:
            available_backups = [
                {
                    "backup_id": b.get("backup_id"),
                    "name": b.get("name"),
                    "date": b.get("date"),
                }
                for b in backups[:5]
            ]
            return {
                "success": False,
                "error": f"Backup '{backup_id}' not found",
                "available_backups": available_backups,
                "suggestion": "Use one of the available backup IDs listed above",
            }

        # Create safety backup BEFORE restoring
        logger.info("Creating safety backup before restore...")
        now = datetime.now()
        safety_backup_name = f"PreRestore_Safety_{now.strftime('%Y-%m-%d_%H:%M:%S')}"

        # Get backup password
        password, error = await _get_backup_password(ws_client)
        if error:
            # Password error - log warning but continue (restore might still work)
            logger.warning("No default password - proceeding without safety backup")
            safety_backup_id = None
        else:
            safety_backup = await ws_client.send_command(
                "backup/generate",
                name=safety_backup_name,
                password=password,
                agent_ids=["hassio.local"],
                include_homeassistant=True,
                include_database=True,  # Full backup for safety
                include_all_addons=True,
            )

            if not safety_backup.get("success"):
                return {
                    "success": False,
                    "error": "Failed to create safety backup before restore",
                    "details": safety_backup,
                    "suggestion": "Cannot proceed with restore without safety backup",
                }

            safety_backup_id = safety_backup.get("result", {}).get("backup_job_id")
            logger.info(f"Safety backup created: {safety_backup_id}")

        # Perform restore
        restore_params = {
            "backup_id": backup_id,
            "agent_id": "hassio.local",
            "restore_database": restore_database,
            "restore_homeassistant": True,
            "restore_addons": [],  # Restore all addons from backup
            "restore_folders": [],  # Restore all folders from backup
        }

        result = await ws_client.send_command("backup/restore", **restore_params)

        if result.get("success"):
            return {
                "success": True,
                "backup_id": backup_id,
                "status": "Restore initiated - Home Assistant will restart",
                "safety_backup_id": safety_backup_id,
                "restore_database": restore_database,
                "warning": "Home Assistant is restarting. Connection will be temporarily lost.",
                "note": "A safety backup was created before restore. You can restore from it if needed.",
            }
        else:
            return {
                "success": False,
                "error": "Restore operation failed",
                "details": result,
                "safety_backup_id": safety_backup_id,
            }

    except Exception as e:
        logger.error(f"Error restoring backup: {e}")
        return {
            "success": False,
            "error": f"Failed to restore backup: {str(e)}",
            "suggestion": "Check Home Assistant connection and backup availability",
        }
    finally:
        # Always disconnect WebSocket
        if ws_client:
            try:
                await ws_client.disconnect()
            except:
                pass  # Ignore errors during cleanup


def register_backup_tools(mcp: "FastMCP", client: HomeAssistantClient) -> None:
    """
    Register backup and restore tools with the MCP server.

    Args:
        mcp: FastMCP server instance
        client: Home Assistant REST client
    """
    from ..utils.usage_logger import log_tool_usage

    # Generate dynamic backup description based on BACKUP_HINT config
    backup_hint_text = get_backup_hint_text()
    backup_create_description = f"""Create a fast Home Assistant backup (local only).

**What's Included:**
- Home Assistant configuration (core settings)
- All add-ons
- SSL certificates
- Database is EXCLUDED for faster backup (excludes historical sensor data, statistics, state history)

**Password:** Uses Home Assistant's default backup password (if configured)

**Storage:** Local only (hassio.local agent)

**Duration:** Typically takes several seconds to complete (without database)

**When to Use:**
{backup_hint_text}

**Example Usage:**
- Before deleting device: ha_backup_create("Before_Device_Delete")
- Before modifying system settings: ha_backup_create("Pre_System_Change")
- Quick safety backup: ha_backup_create()

**Returns:** Backup ID and job status"""

    @mcp.tool(description=backup_create_description)
    @log_tool_usage
    async def ha_backup_create(
        name: Annotated[
            str | None,
            Field(
                description="Backup name (auto-generated if not provided, e.g., 'MCP_Backup_2025-10-05_04:30')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create a fast Home Assistant backup (local only)."""
        return await create_backup(client, name)

    @mcp.tool(
        annotations={
            "destructiveHint": True,
        }
    )
    @log_tool_usage
    async def ha_backup_restore(
        backup_id: Annotated[
            str,
            Field(
                description="Backup ID to restore (e.g., 'dd7550ed' from backup list or ha_backup_create result)"
            ),
        ],
        restore_database: Annotated[
            bool,
            Field(
                description="Restore database (default: false for config-only restore)",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Restore Home Assistant from a backup (LAST RESORT - use with extreme caution).

        **⚠️ WARNING - DESTRUCTIVE OPERATION ⚠️**

        **This tool restarts Home Assistant and restores configuration to a previous state.**

        **IMPORTANT CONSIDERATIONS:**
        1. **Try undo operations first** - Often you can just reverse what you did:
           - Deleted automation? Recreate it with ha_config_set_automation
           - Modified script? Use ha_config_set_script to fix it
           - Most config changes can be rolled back without using restore

        2. **Safety mechanism:** A NEW backup is automatically created BEFORE restore
           - This allows you to rollback the restore if needed
           - You can restore from this pre-restore backup if something goes wrong

        3. **What gets restored:**
           - Home Assistant configuration (automations, scripts, etc.)
           - Add-ons (if they were in the backup)
           - Optional: Database - historical sensor data, statistics, state history (set restore_database=true)

        4. **Side effects:**
           - Home Assistant will RESTART during restore
           - Any changes made after the backup was created will be LOST
           - Temporary disconnection from all integrations during restart

        **Recommended workflow:**
        1. Try to undo your changes manually first
        2. If you must restore, use the most recent backup
        3. Set restore_database=false unless you need historical data
        4. Expect a restart and temporary downtime

        **Example Usage:**
        - Restore config only: ha_backup_restore("dd7550ed")
        - Full restore with DB: ha_backup_restore("dd7550ed", restore_database=true)

        **Returns:** Restore job status
        """
        return await restore_backup(client, backup_id, restore_database)
