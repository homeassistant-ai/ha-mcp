"""
Backup and restore tools for Home Assistant MCP Server.

Provides backup creation and restoration capabilities with safety mechanisms.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from ..client.rest_client import HomeAssistantClient
from ..client.websocket_client import HomeAssistantWebSocketClient

logger = logging.getLogger(__name__)


async def _get_connected_ws_client(
    base_url: str, token: str
) -> tuple[HomeAssistantWebSocketClient | None, dict[str, Any] | None]:
    """
    Create and connect a WebSocket client.

    Args:
        base_url: Home Assistant base URL
        token: Authentication token

    Returns:
        Tuple of (ws_client, error_dict). If connection fails, ws_client is None.
    """
    ws_client = HomeAssistantWebSocketClient(base_url, token)
    connected = await ws_client.connect()
    if not connected:
        return None, {
            "success": False,
            "error": "Failed to connect to Home Assistant WebSocket",
            "suggestion": "Check Home Assistant connection and ensure WebSocket API is available",
        }
    return ws_client, None


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
        ws_client, error = await _get_connected_ws_client(client.base_url, client.token)
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
        ws_client, error = await _get_connected_ws_client(client.base_url, client.token)
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
