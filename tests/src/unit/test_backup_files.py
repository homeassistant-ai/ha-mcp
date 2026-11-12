"""A file snapshot keeps line endings as stored when the component can.

Home Assistant's config directory is this test's ``tmp_path``, and Core sets
the File & YAML Tools entry up, which registers its ``read_file`` service and
the ``ha_mcp_tools/*`` commands the server reads the component's capabilities
from. Kept out of ``test_backup_manager.py``, which is at its module-size
baseline.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.const import DOMAIN, TOOLS_ENTRY_TITLE
from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp import backup_files
from ha_mcp.backup_manager import BackupManager, get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient

CRLF = "a: 1\r\nb: 2\r\n"


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Core's config directory is this test's ``tmp_path``."""
    return hass_tmp_config_dir


@pytest.fixture
def crlf_files(hass_config_dir: str) -> None:
    """A stylesheet and ``configuration.yaml`` saved with CRLF line endings."""
    for path in ("www/a.css", "configuration.yaml"):
        target = Path(hass_config_dir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CRLF.encode())


@pytest.fixture
async def manager(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    crlf_files: None,
) -> BackupManager:
    """A backup manager on a ``hass`` with the File & YAML Tools entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TOOLS_ENTRY_TITLE,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path / "backups"),
    )
    return get_backup_manager(core_client, settings)


async def _snapshot_content(manager: BackupManager, domain: str, path: str) -> str:
    captured = await manager.maybe_snapshot(domain, path, force=True)
    assert captured is not None
    content = manager.read_snapshot(captured.name)["config"]
    assert isinstance(content, str)
    return content


@pytest.mark.parametrize(
    ("domain", "path"), [("file", "www/a.css"), ("yaml_file", "configuration.yaml")]
)
async def test_a_file_snapshot_restores_crlf_exactly(
    manager: BackupManager, domain: str, path: str
) -> None:
    """A snapshot that turned CRLF into LF would restore a different file."""
    assert await _snapshot_content(manager, domain, path) == CRLF


async def test_older_component_is_not_sent_the_field(
    manager: BackupManager,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its strict read_file schema would refuse the call, and with it the write."""
    # A component from before the flag: it does not advertise it.
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [c for c in system.CAPABILITIES if c != backup_files.LINE_ENDINGS_CAPABILITY],
    )
    with patch.object(
        core_client, "call_service", wraps=core_client.call_service
    ) as spy:
        content = await _snapshot_content(manager, "file", "www/a.css")

    reads = [
        c.args[2] for c in spy.call_args_list if c.args[:2] == (DOMAIN, "read_file")
    ]
    assert content == "a: 1\nb: 2\n"
    assert reads
    assert all("keep_line_endings" not in data for data in reads)
