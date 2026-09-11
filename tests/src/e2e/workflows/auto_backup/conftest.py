"""Keep in-process edit-backup tests out of the developer's backup history."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def isolate_edit_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ha_container_with_fresh_config: dict[str, Any],
) -> Iterator[None]:
    """Use a fresh backup directory wherever the server runs inside pytest."""
    if ha_container_with_fresh_config.get("backend") in (
        "embedded",
        "haos_inaddon",
        "haos_embedded",
        "haos_stdio",
    ):
        # Remote servers and stdio own their environment; host paths do not apply.
        yield
        return

    from ha_mcp.config import _reset_global_settings

    # HA_MCP_CONFIG_DIR alone does not override an existing legacy backup folder.
    try:
        with monkeypatch.context() as isolated:
            isolated.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
            _reset_global_settings()
            yield
    finally:
        _reset_global_settings()
