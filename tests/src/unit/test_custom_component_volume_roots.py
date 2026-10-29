"""/backup is not an HAOS sibling-volume root for the file tools (#2875).

Home Assistant Core's container mounts /share and /media read-write and /ssl
read-only; /backup is never mounted into it, so a configured /backup entry
would make ha_write_file create the directory inside the container and report
success for a file that never reaches backup storage.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.persistent_notification"] = MagicMock()
sys.modules["homeassistant.config"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules.setdefault("homeassistant.core", MagicMock())
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.loader"] = MagicMock()

import custom_components.ha_mcp_tools as comp  # noqa: E402
from custom_components.ha_mcp_tools.const import ALLOWED_VOLUME_ROOTS  # noqa: E402


def test_backup_is_not_offered_as_a_volume_root():
    assert "/backup" not in ALLOWED_VOLUME_ROOTS
    assert comp._volume_root_for("/backup") is None
    assert comp._volume_root_for("/backup/abc.tar") is None


def test_backup_entry_is_rejected(tmp_path):
    assert comp._normalize_extra_dir("/backup", tmp_path) is None
    assert comp._normalize_extra_dir("/backup/llm", tmp_path) is None


async def test_stored_backup_entry_is_dropped_on_load(monkeypatch, tmp_path):
    # Saved before /backup stopped being a root: dropped, with the existing
    # load-time warning; the other volume entries survive.
    stored = {"paths": ["/share/llm", "/backup", "/backup/x"]}
    monkeypatch.setattr(
        comp,
        "Store",
        lambda *a, **k: MagicMock(async_load=AsyncMock(return_value=stored)),
    )
    hass = MagicMock()
    hass.config.config_dir = str(tmp_path)
    assert await comp._load_allowed_paths(hass) == ["/share/llm"]
