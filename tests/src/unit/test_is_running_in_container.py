"""Direct tests for ``_is_running_in_container`` detection.

Every ``_warn_if_default_path_exposed`` test mocks this predicate, so the
actual detection mechanism — the ``/.dockerenv`` / ``/run/.containerenv``
marker files and the ``SUPERVISOR_TOKEN`` env var that suppress the
default-path warning on containerized / add-on deployments — is never
exercised there. A wrong marker path or env-var name would pass that suite
green. These tests pin the real signals.
"""

from __future__ import annotations

import pytest

from ha_mcp.__main__ import _is_running_in_container


@pytest.mark.parametrize("marker", ["/.dockerenv", "/run/.containerenv"])
def test_detects_container_marker_file(
    marker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr("os.path.exists", lambda path: path == marker)
    assert _is_running_in_container() is True


def test_detects_ha_addon_via_supervisor_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.path.exists", lambda _: False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "abc123")
    assert _is_running_in_container() is True


def test_no_markers_no_token_is_not_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("os.path.exists", lambda _: False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert _is_running_in_container() is False


def test_empty_supervisor_token_is_not_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``SUPERVISOR_TOKEN`` must not count as containerized."""
    monkeypatch.setattr("os.path.exists", lambda _: False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "")
    assert _is_running_in_container() is False
