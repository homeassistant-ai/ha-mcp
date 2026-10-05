"""The explicit ``ha_manage_backup(edits, create)`` capture."""

from pathlib import Path

from ..backup_manager import BackupManager, MandatoryBackupError
from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error


async def capture_on_demand(mgr: BackupManager, domain: str, entity_id: str) -> Path:
    """Snapshot ``domain:entity_id`` now, or raise a structured error.

    Mandatory, so a fetch that fails is reported as such (a flow helper the
    component cannot read, an old component without subentry data) and only a
    target that does not exist is "not found".
    """
    try:
        path = await mgr.maybe_snapshot(
            domain,
            entity_id,
            tool_name="ha_manage_backup.edits.create",
            force=True,
            mandatory=True,
        )
    except MandatoryBackupError as err:
        cause = err.__cause__
        raise_tool_error(
            create_error_response(
                ErrorCode.BACKUP_CAPTURE_FAILED,
                f"Could not snapshot {domain}:{entity_id}: "
                + (
                    err.safe_detail
                    or "its current configuration could not be read "
                    f"({type(cause).__name__ if cause else 'capture failed'})"
                ),
                context={"domain": domain, "entity_id": entity_id},
                suggestions=err.suggestions
                or ["Check the server log for the capture failure"],
            )
        )
    if path is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Could not snapshot {domain}:{entity_id} — entity not found "
                "or fetch returned no config",
                context={"domain": domain, "entity_id": entity_id},
                suggestions=[
                    "Verify the entity exists via the matching ha_config_get_* tool",
                    "For helpers, pass domain='helper_<helper_type>', e.g. "
                    + "'helper_input_boolean'",
                ],
            )
        )
    return path
