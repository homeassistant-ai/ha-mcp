"""Read Core's faulthandler output on either component entry."""

from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.__main__ import FAULT_LOG_FILENAME
from homeassistant.core import HomeAssistant

from .core_log import read_log_tail_sync


def _read(path: Path, lines: int) -> dict[str, Any]:
    try:
        result = read_log_tail_sync(path, lines)
    except OSError as exc:
        return {"path": FAULT_LOG_FILENAME, "error": str(exc)}
    if result.get("_error") in {"not_found", "not_a_file"}:
        return {"path": FAULT_LOG_FILENAME, "total_lines": 0, "content": ""}
    return {
        **result,
        "path": FAULT_LOG_FILENAME,
        "modified": datetime.fromtimestamp(result["mtime"]).isoformat(),
    }


async def _prepare(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    path = Path(hass.config.path(FAULT_LOG_FILENAME))
    return await hass.async_add_executor_job(_read, path, msg["lines"])


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    return [
        (
            {
                vol.Required("type"): "ha_mcp_tools/fault_log",
                vol.Required("lines"): vol.All(int, vol.Range(min=1, max=2000)),
            },
            lambda hass, msg, **result: result,
            _prepare,
        )
    ]
