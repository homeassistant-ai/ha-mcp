"""Unit tests for the shared data-directory resolver.

The resolver is the single source of truth for where ha-mcp writes its
persistent files (tool config, usage logs). These tests pin its priority
order and the fallback behavior added for issue #1125.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from ha_mcp.utils.data_paths import _resolve_data_dir, get_data_dir


@pytest.fixture(autouse=True)
def _reset_cache():
    """Force every test to re-resolve from scratch."""
    get_data_dir.cache_clear()
    yield
    get_data_dir.cache_clear()


def _failing_os(**overrides):
    """Return an ``os`` stand-in that fails only the named calls.

    Injected as ``data_paths.os`` so the failure is scoped to the module under
    test — monkeypatching an attribute on the real ``os`` module would apply
    process-wide for the duration of the call.
    """

    class _ShimOS:
        def __getattr__(self, name):
            if name in overrides:
                return overrides[name]
            return getattr(os, name)

    return _ShimOS()


def _stub_write_probe(monkeypatch, writable: set[Path]) -> None:
    """Answer the write probe from ``writable`` for paths that don't exist here.

    The resolver probes each candidate with a real ``mkstemp``. Tests that
    fake a non-existent directory (``/data`` on a dev box) have to fake the
    probe too, or it fails on the missing path and the candidate looks
    unwritable. Real ``tmp_path`` directories still get a real probe.
    """
    from ha_mcp.utils import data_paths

    original = data_paths._try_write

    def fake_write(path: Path) -> OSError | None:
        if path in writable:
            return None
        return original(path)

    monkeypatch.setattr(data_paths, "_try_write", fake_write)


class TestPriorityOrder:
    """HA_MCP_CONFIG_DIR > /data > ~/.ha-mcp > tempdir/ha-mcp."""

    def test_addon_path_when_supervisor_token_set(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        # ``/data`` is not present on dev/CI machines; stub both halves of the
        # writability check rather than relying on the real path.
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self == Path("/data"):
                return None
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        _stub_write_probe(monkeypatch, writable={Path("/data")})
        assert get_data_dir() == Path("/data")

    def test_home_path_when_no_supervisor_token(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = get_data_dir()
        assert result == tmp_path / ".ha-mcp"
        assert result.is_dir()

    def test_ha_mcp_config_dir_overrides_supervisor_token(self, monkeypatch, tmp_path):
        """HA_MCP_CONFIG_DIR takes precedence even in add-on mode.

        Lets add-on users override the default ``/data`` location, and lets
        hardened-Docker users bind-mount a writable volume without depending
        on ``$HOME``.
        """
        custom_dir = tmp_path / "custom"
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(custom_dir))
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")  # would normally route to /data
        result = get_data_dir()
        assert result == custom_dir
        assert custom_dir.is_dir()

    def test_whitespace_only_ha_mcp_config_dir_is_ignored(self, monkeypatch, tmp_path):
        """``HA_MCP_CONFIG_DIR="   "`` (e.g. trailing space in a .env file) is
        truthy but ``Path("   ")`` resolves cwd-relative — without
        ``.strip()`` the resolver would mkdir a literal three-space-named
        directory. Whitespace-only must be treated as unset.

        The "nothing was created" half compares a listing of the working
        directory rather than probing ``cwd / "   "`` directly: Windows strips
        trailing spaces from path components, so that path resolves back to
        the working directory itself and ``.exists()`` is unconditionally
        true there — the probe reported a failure on Windows no matter how the
        resolver behaved. A listing diff expresses the same intent and holds
        on both platforms.
        """
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", "   ")
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cwd = Path.cwd()
        before = set(cwd.iterdir())

        result = get_data_dir()

        assert result == tmp_path / ".ha-mcp"
        assert set(cwd.iterdir()) == before, (
            "resolver created something next to the working directory for a "
            "whitespace-only HA_MCP_CONFIG_DIR"
        )


class TestFallbacks:
    """Falls back to a writable tmpdir when the preferred location fails."""

    def test_falls_back_to_tmpdir_when_home_unwritable(self, monkeypatch, tmp_path):
        """Issue #1125 regression: ``read_only: true`` Docker, or ``HOME=/``.

        ``mkdir(~/.ha-mcp)`` raises ``OSError(EROFS)``; resolver must fall
        through to ``<tempdir>/ha-mcp`` instead of crashing.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        readonly_home = tmp_path / "readonly-home"
        readonly_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: readonly_home)
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self == readonly_home / ".ha-mcp":
                raise OSError(30, "Read-only file system")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )

        result = get_data_dir()

        assert result == fallback_root / "ha-mcp"
        assert result.is_dir()

    def test_ha_mcp_config_dir_unwritable_chains_to_tmpdir(self, monkeypatch, tmp_path):
        """HA_MCP_CONFIG_DIR mkdir failure chains to tmpdir, doesn't return broken path."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        readonly_parent = tmp_path / "readonly-parent"
        readonly_parent.mkdir()
        broken_target = readonly_parent / "cannot-create"
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self == broken_target:
                raise OSError(30, "Read-only file system")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(broken_target))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "unused-home")
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )

        result = get_data_dir()

        assert result == fallback_root / "ha-mcp"
        assert result.is_dir()
        assert not broken_target.exists()

    def test_ha_mcp_config_dir_unwritable_in_addon_mode_chains_to_tmpdir(
        self, monkeypatch, tmp_path
    ):
        """Silent-override-discard regression: ``HA_MCP_CONFIG_DIR`` mkdir
        fails AND ``SUPERVISOR_TOKEN`` is set. The resolver must NOT
        silently write to ``/data`` — the user picked the override
        deliberately, so fall through to the tmpdir instead.
        """
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        readonly_parent = tmp_path / "readonly-parent"
        readonly_parent.mkdir()
        broken_target = readonly_parent / "cannot-create"
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self == broken_target:
                raise OSError(30, "Read-only file system")
            if self == Path("/data"):
                return None  # /data is "writable" — must still be skipped
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        # /data must look fully usable — the point is that it's skipped
        # anyway, not that it happens to be unwritable on this machine.
        _stub_write_probe(monkeypatch, writable={Path("/data")})
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(broken_target))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "unused-home")
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )

        result = get_data_dir()

        assert result == fallback_root / "ha-mcp"
        assert result.is_dir()

    def test_falls_back_when_addon_data_dir_unwritable(self, monkeypatch, tmp_path):
        """Residual same-class bug: ``/data`` may be read-only (degraded
        supervisor, or supervisor container running with ``read_only:
        true``). With no ``HA_MCP_CONFIG_DIR``, the resolver must fall
        through to the home/tmpdir chain instead of returning a path the
        first ``write_text`` will crash on.
        """
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        readonly_home = tmp_path / "readonly-home"
        readonly_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: readonly_home)
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self in (Path("/data"), readonly_home / ".ha-mcp"):
                raise OSError(30, "Read-only file system")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)

        result = get_data_dir()

        assert result == fallback_root / "ha-mcp"
        assert result.is_dir()

    def test_existing_but_unwritable_dir_is_rejected(
        self, monkeypatch, tmp_path, caplog
    ):
        """Issue #2078: the mount point exists, but the container can't write it.

        ``Path.mkdir(exist_ok=True)`` reports success for any directory that
        already exists, whoever owns it. Every Docker deployment hits that
        branch — the image ships ``~/.ha-mcp`` and Docker creates the mount
        point when attaching a volume — so without a real write probe a
        root-owned volume resolves as a good data dir, no warning is logged,
        and persistence silently disappears. Simulated by failing the probe,
        because the test process can't make a directory unwritable to itself
        on every platform (root ignores mode bits; Windows ignores chmod).
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        home = tmp_path / "home"
        home.mkdir()
        # The mount point exists and mkdir will happily "succeed" on it.
        (home / ".ha-mcp").mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)

        from ha_mcp.utils import data_paths

        original = data_paths._try_write

        def fake_write(path: Path) -> OSError | None:
            if path == home / ".ha-mcp":
                return OSError(13, "Permission denied")
            return original(path)

        monkeypatch.setattr(data_paths, "_try_write", fake_write)
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.data_paths"):
            result = get_data_dir()

        assert result == fallback_root / "ha-mcp"
        assert [r for r in caplog.records if "Falling back" in r.getMessage()], (
            "unwritable data dir must warn, not resolve silently"
        )

    def test_write_probe_accepts_a_writable_dir_and_leaves_nothing_behind(
        self, tmp_path
    ):
        """The probe must not litter the data dir with leftover files."""
        from ha_mcp.utils.data_paths import _try_write

        assert _try_write(tmp_path) is None
        assert list(tmp_path.iterdir()) == []

    def test_write_probe_reports_the_error_for_a_missing_dir(self, tmp_path):
        """A candidate that isn't there at all is a probe failure, not a crash."""
        from ha_mcp.utils.data_paths import _try_write

        assert isinstance(_try_write(tmp_path / "nope"), OSError)

    def test_write_probe_rejects_a_dir_it_cannot_clean_up(self, monkeypatch, tmp_path):
        """create-succeeds-then-unlink-fails must fail the probe, not pass it.

        Removing the probe is part of the check: the real writers finish with
        ``os.replace``, which needs the same directory permission ``unlink``
        does, so a share or ACL granting create but denying unlink (some
        SMB/NFS exports) cannot persist anything. Guards against the cleanup
        being wrapped back into ``contextlib.suppress``, which would silently
        accept such a directory again.
        """
        from ha_mcp.utils import data_paths

        attempts = []

        def fake_unlink(target):
            attempts.append(target)
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(data_paths, "os", _failing_os(unlink=fake_unlink))
        result = data_paths._try_write(tmp_path)

        assert isinstance(result, OSError), (
            "a directory whose probe cannot be removed must be rejected"
        )
        assert attempts, "the probe should have attempted to remove itself"

    def test_write_probe_reports_a_close_failure_instead_of_raising(
        self, monkeypatch, tmp_path
    ):
        """``os.close`` blowing up must not escape and abort startup.

        ``_resolve_data_dir`` runs during server start, so an exception
        escaping the probe would crash instead of falling back to a tmpdir.
        """
        from ha_mcp.utils import data_paths

        def fake_close(fd):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(data_paths, "os", _failing_os(close=fake_close))
        result = data_paths._try_write(tmp_path)

        assert isinstance(result, OSError)

    def test_returns_unwritable_tmpdir_when_everything_fails(
        self, monkeypatch, tmp_path, caplog
    ):
        """Last-resort branch: env mkdir fails, home mkdir fails, tmpdir mkdir
        fails. The resolver returns the tmpdir path anyway so callers (which
        wrap their own writes in ``try/except OSError``) can degrade
        gracefully rather than crashing the server. Logged at ERROR because
        persistence is silently disabled — operator-visible state, not just
        a warning.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        readonly_home = tmp_path / "ro-home"
        readonly_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: readonly_home)
        fake_tmp = tmp_path / "ro-tmp"
        fake_tmp.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fake_tmp)
        )
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self in (readonly_home / ".ha-mcp", fake_tmp / "ha-mcp"):
                raise OSError(30, "Read-only file system")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.data_paths"):
            result = get_data_dir()

        assert result == fake_tmp / "ha-mcp"
        persistence_records = [
            r for r in caplog.records if "persistence is disabled" in r.getMessage()
        ]
        assert persistence_records, "expected a persistence-disabled log record"
        assert all(r.levelno == logging.ERROR for r in persistence_records)


class TestMemoization:
    """The resolver must memoize so warnings emit once at startup."""

    def test_warning_emitted_only_once_via_module_cache(
        self, monkeypatch, tmp_path, caplog
    ):
        """``get_data_dir`` is hit on every settings UI HTTP request and on
        every usage-log write — without the cache the fallback warning
        would spam logs on every UI toggle.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        readonly_home = tmp_path / "readonly-home"
        readonly_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: readonly_home)
        original_mkdir = Path.mkdir

        def fake_mkdir(self: Path, *args, **kwargs):
            if self == readonly_home / ".ha-mcp":
                raise OSError(30, "Read-only file system")
            return original_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        fallback_root = tmp_path / "fallback-tmp"
        fallback_root.mkdir()
        monkeypatch.setattr(
            "ha_mcp.utils.data_paths.tempfile.gettempdir", lambda: str(fallback_root)
        )

        with caplog.at_level(logging.WARNING, logger="ha_mcp.utils.data_paths"):
            for _ in range(5):
                get_data_dir()

        fallback_warnings = [
            r for r in caplog.records if "Falling back" in r.getMessage()
        ]
        assert len(fallback_warnings) == 1, (
            f"expected single fallback warning, got {len(fallback_warnings)}"
        )

    def test_resolve_re_runs_when_cache_cleared(self, monkeypatch, tmp_path):
        """``_resolve_data_dir`` (uncached) re-runs on every call.

        Guards against future refactors that move the cache or accidentally
        memoize the inner function.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(first))
        assert _resolve_data_dir() == first

        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(second))
        assert _resolve_data_dir() == second
