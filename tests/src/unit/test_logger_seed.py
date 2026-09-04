"""The seeded-config logger merge used by the HAOS deadlock e2e (#2357/#2361).

Three branches, all pure string work, so they are pinned here rather than
discovered inside a 75-minute HAOS lane the first time the seed changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_E2E_UTILITIES = Path(__file__).resolve().parents[1] / "e2e" / "utilities"
if str(_E2E_UTILITIES) not in sys.path:
    sys.path.insert(0, str(_E2E_UTILITIES))

from logger_seed import with_probe_logger_config  # noqa: E402

_COMPONENT = "\nreentrant_log_probe:\n"
_ENTRY = "    custom_components.reentrant_log_probe: debug\n"


def test_merges_into_the_seeds_logs_mapping() -> None:
    seed = "default_config:\n\nlogger:\n  logs:\n    custom_components.ha_mcp_tools: info\n"

    merged = with_probe_logger_config(seed, _COMPONENT, _ENTRY)

    assert merged.count("\nlogger:") == 1
    assert yaml.safe_load(merged)["logger"]["logs"] == {
        "custom_components.ha_mcp_tools": "info",
        "custom_components.reentrant_log_probe": "debug",
    }
    assert "reentrant_log_probe" in yaml.safe_load(merged)


def test_appends_a_whole_block_when_the_seed_has_none() -> None:
    merged = with_probe_logger_config("default_config:\n", _COMPONENT, _ENTRY)

    assert yaml.safe_load(merged)["logger"]["logs"] == {
        "custom_components.reentrant_log_probe": "debug"
    }


def test_refuses_a_logger_block_without_a_logs_mapping() -> None:
    seed = "logger:\n  default: warning\n\nfrontend:\n"

    with pytest.raises(AssertionError, match="no `logs:` mapping"):
        with_probe_logger_config(seed, _COMPONENT, _ENTRY)


def test_the_real_seed_merges_cleanly() -> None:
    """The committed seed is the shape the HAOS lane will actually hand over."""
    seed = (
        Path(__file__).resolve().parents[2]
        / "initial_test_state"
        / "configuration.yaml"
    ).read_text(encoding="utf-8")

    merged = with_probe_logger_config(seed, _COMPONENT, _ENTRY)

    assert merged.count("\nlogger:") == 1
    assert "custom_components.reentrant_log_probe: debug" in merged
