"""The unit suite logs at DEBUG whatever the verbosity.

Core's test plugin turns DEBUG on only when pytest runs verbose, so without the
suite's own setting a quiet run takes other code paths than a verbose one.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

QUIET_RUN = "HA_MCP_UNIT_LOG_LEVEL_QUIET_RUN"
REPO = Path(__file__).resolve().parents[3]


def test_a_quiet_run_logs_at_debug() -> None:
    """Checked in a quiet pytest of its own: this run may be verbose, where
    Core's plugin sets the level and a missing setting would not show."""
    if os.environ.get(QUIET_RUN):
        assert logging.getLogger().level == logging.DEBUG
        return
    env = {k: v for k, v in os.environ.items() if k != "UNIT_TEST_SHARD"}
    quiet = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", __file__],
        cwd=REPO,
        env={**env, QUIET_RUN: "1"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert quiet.returncode == 0, quiet.stdout[-2000:] + quiet.stderr[-2000:]
