"""Regression tests for the demo-server setup script."""

from pathlib import Path

SCRIPT = Path(__file__).with_name("setup-ha-mcp.sh")


def test_setup_recovers_incomplete_checkout() -> None:
    content = SCRIPT.read_text()

    assert '[[ -e "$SETUP_HOME/ha-mcp" && ! -d "$SETUP_HOME/ha-mcp/.git" ]]' in content
    assert 'mv "$SETUP_HOME/ha-mcp" "$BROKEN_REPO"' in content
    assert 'git -C "$SETUP_HOME/ha-mcp" pull --ff-only || true' not in content


def test_setup_removes_all_legacy_cron_variants() -> None:
    content = SCRIPT.read_text()

    assert 'remove_legacy_cron_entries "$SETUP_USER"' in content
    assert "remove_legacy_cron_entries root" in content
    assert "ha-mcp-cron\\.log" in content
    assert "hamcp-test-env" in content
