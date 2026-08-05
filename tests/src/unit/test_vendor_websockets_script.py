"""Unit tests for scripts/vendor_websockets.py.

The sync script is what makes the vendored copy trustworthy, so its
failure paths matter as much as its happy path: it writes into the
package that ships to every user, from an archive downloaded at run time.

Each test drives ``main()`` against a locally built tarball with the
download patched out — no network, no real PyPI.
"""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import vendor_websockets as vendor  # noqa: E402

_VERSION = "9.9.9"
_PREFIX = f"websockets-{_VERSION}"


def _tarball(members: dict[str, str]) -> bytes:
    """Build an in-memory sdist containing exactly ``members``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in members.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _sdist(extra: dict[str, str] | None = None) -> bytes:
    members = {
        f"{_PREFIX}/LICENSE": "BSD-3\n",
        f"{_PREFIX}/src/websockets/__init__.py": "__version__ = '9.9.9'\n",
        f"{_PREFIX}/src/websockets/asyncio/client.py": "connect = None\n",
        f"{_PREFIX}/src/websockets/speedups.c": "/* native */\n",
        f"{_PREFIX}/src/websockets/speedups.pyi": "def apply_mask(): ...\n",
    }
    members.update(extra or {})
    return _tarball(members)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the script at a throwaway vendor dir and a fake download."""
    vendor_dir = tmp_path / "_vendor"
    target = vendor_dir / "websockets"
    vendor_dir.mkdir()
    (vendor_dir / "requirements.txt").write_text(
        f"websockets=={_VERSION}\n", encoding="utf-8"
    )
    monkeypatch.setattr(vendor, "_VENDOR_DIR", vendor_dir)
    monkeypatch.setattr(vendor, "_PIN_FILE", vendor_dir / "requirements.txt")
    monkeypatch.setattr(vendor, "_TARGET", target)

    def install(payload: bytes) -> None:
        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        monkeypatch.setattr(
            vendor.urllib.request, "urlopen", lambda *a, **k: _Response(payload)
        )

    return target, install


class TestHappyPath:
    def test_vendors_sources_and_records_a_manifest(self, sandbox, capsys):
        target, install = sandbox
        install(_sdist())

        assert vendor.main() == 0

        assert (target / "__init__.py").is_file()
        assert (target / "asyncio" / "client.py").is_file()
        assert (target / "LICENSE").is_file()
        assert f"websockets=={_VERSION}" in (target / "VENDORED").read_text(
            encoding="utf-8"
        )
        recorded = (
            (target / vendor.MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
        )
        assert recorded == vendor.manifest_lines(target)

    def test_compiled_artifacts_and_their_stub_are_not_vendored(self, sandbox):
        """Pure-Python only — and no stub for a module we do not ship."""
        target, install = sandbox
        install(_sdist())

        assert vendor.main() == 0

        assert not (target / "speedups.c").exists()
        assert not (target / "speedups.pyi").exists()

    def test_a_resync_replaces_the_previous_tree(self, sandbox):
        target, install = sandbox
        install(_sdist())
        assert vendor.main() == 0
        stale = target / "gone_next_time.py"
        stale.write_text("# left over from an older release\n", encoding="utf-8")

        install(_sdist())
        assert vendor.main() == 0

        assert not stale.exists(), "the swap must not merge into the old tree"


class TestFailurePaths:
    def test_archive_member_escaping_the_vendor_dir_is_refused(self, sandbox):
        """CWE-22: a crafted member must never write outside the tree."""
        target, install = sandbox
        install(_sdist({f"{_PREFIX}/src/websockets/../../../evil.py": "pwned = 1\n"}))

        with pytest.raises(SystemExit):
            vendor.main()

        assert not (target.parent.parent / "evil.py").exists()

    def test_a_failed_sync_leaves_no_staging_tree_and_no_live_tree(self, sandbox):
        """A refused member must not strand a half-written package."""
        target, install = sandbox
        install(_sdist({f"{_PREFIX}/src/websockets/../../../evil.py": "pwned = 1\n"}))

        with pytest.raises(SystemExit):
            vendor.main()

        staging = target.with_name(target.name + ".incoming")
        assert not staging.exists(), "staging tree left behind after a failure"

    def test_a_failed_sync_does_not_touch_the_existing_tree(self, sandbox):
        """The live vendored package survives a failed re-sync intact."""
        target, install = sandbox
        install(_sdist())
        assert vendor.main() == 0
        before = vendor.manifest_lines(target)

        install(_sdist({f"{_PREFIX}/src/websockets/../../../evil.py": "pwned = 1\n"}))
        with pytest.raises(SystemExit):
            vendor.main()

        assert vendor.manifest_lines(target) == before

    def test_unexpected_sdist_layout_is_refused(self, sandbox):
        """No websockets/__init__.py means the layout moved — bail loudly."""
        target, install = sandbox
        install(_tarball({f"{_PREFIX}/LICENSE": "BSD-3\n"}))

        with pytest.raises(SystemExit):
            vendor.main()

    def test_missing_pin_is_refused(self, sandbox, monkeypatch):
        target, _install = sandbox
        vendor._PIN_FILE.write_text("# no pin here\n", encoding="utf-8")

        with pytest.raises(SystemExit):
            vendor.read_pin()
