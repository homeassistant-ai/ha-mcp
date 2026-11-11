"""The app's (add-on's) shutdown path, which takes the OAuth surface dark.

``start.py`` is the app's own process, outside Core. A Supervisor stop sends
SIGTERM; the cleanup must run, unlink the heartbeat the integration's OAuth
views check before anything slow, and drop the inbound-log mirror.
"""

from __future__ import annotations

import signal
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from ._variants import Variant, load_start, script_function


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_inbound_log_tail_starts_after_existing_history(
    variant: Variant, tmp_path: Path
) -> None:
    """A restart must not replay the mirror file's old lines into the log."""
    start = load_start(variant)
    initial_tail_offset = script_function(start, "_initial_tail_offset")
    log_file = tmp_path / "inbound.log"

    with patch.object(start, "INBOUND_LOG_FILE", log_file):
        assert initial_tail_offset() == 0
        log_file.write_bytes(b"hello\n")
        assert initial_tail_offset() == log_file.stat().st_size


def test_stop_signal_records_its_name_and_interrupts_the_loop(
    variant: Variant, restore_signal_handlers: None
) -> None:
    """Without a handler a Supervisor stop killed the process mid-loop, so the
    cleanup never ran and the log never said why it exited (#1694)."""
    start = load_start(variant)

    reason = script_function(start, "_install_shutdown_handlers")()
    handler = signal.getsignal(signal.SIGTERM)

    assert callable(handler)
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)
    assert reason == {"reason": "SIGTERM"}


def test_shutdown_cleanup_takes_oauth_dark_before_removing_the_entry(
    variant: Variant, tmp_path: Path, restore_signal_handlers: None
) -> None:
    """The heartbeat goes first, so a kill during the slow Core calls cannot
    leave the OAuth views serving for a stopped app; then the entry is removed,
    the mirror dropped, and a second signal can no longer abort the cleanup."""
    start = load_start(variant)
    log_file = tmp_path / "inbound.log"
    log_file.write_text("x\n")
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text("")
    heartbeat_during_removal: list[bool] = []
    logged: list[str] = []

    with (
        patch.object(start, "INBOUND_LOG_FILE", log_file),
        patch.object(start, "HEARTBEAT_FILE", heartbeat),
        patch.object(
            start,
            "_remove_config_entry",
            side_effect=lambda: heartbeat_during_removal.append(heartbeat.exists()),
        ),
        patch.object(start, "log_info", side_effect=logged.append),
    ):
        script_function(start, "_shutdown_cleanup")("SIGTERM")

    assert heartbeat_during_removal == [False]
    assert not heartbeat.exists()
    assert not log_file.exists()
    assert any("reason: SIGTERM" in line for line in logged)
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
