"""Refuse an unusable HACS frontend before starting test containers."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import pytest


@pytest.fixture
def seed() -> ModuleType:
    from tests.src.e2e import _conftest_seed

    return _conftest_seed


@pytest.fixture
def state(tmp_path: Path) -> Path:
    (tmp_path / "custom_components" / "hacs").mkdir(parents=True)
    return tmp_path


def _frontend(state: Path) -> Path:
    return state / "custom_components" / "hacs" / "hacs_frontend"


def _archive(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, payload in files.items():
            member = tarfile.TarInfo(f"hacs_frontend-1.0.0/{name}")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return stream.getvalue()


def _serve_archive(
    seed: ModuleType, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    responses = iter([json.dumps({"tag_name": "1.0.0"}).encode(), payload])

    def urlopen(url: str, timeout: int) -> io.BytesIO:
        return io.BytesIO(next(responses))

    monkeypatch.setattr(seed.urllib.request, "urlopen", urlopen)


def test_download_failure_aborts_bootstrap_and_releases_lock(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> NoReturn:
        raise urllib.error.URLError("fixture network unavailable")

    monkeypatch.setattr(seed.urllib.request, "urlopen", unavailable)
    with pytest.raises(urllib.error.URLError, match="fixture network unavailable"):
        seed._ensure_hacs_frontend(state)
    assert not (state / ".hacs_frontend.lock").exists()


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"not a gzip archive", tarfile.TarError),
        (_archive({"other.txt": b"unrelated"}), RuntimeError),
        (_archive({"hacs_frontend/entrypoint.js": b"export {};"}), RuntimeError),
    ],
    ids=["corrupt-archive", "missing-package", "missing-python-module"],
)
def test_unusable_archive_aborts_bootstrap(
    seed: ModuleType,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    error: type[Exception],
) -> None:
    _serve_archive(seed, monkeypatch, payload)
    with pytest.raises(error):
        seed._ensure_hacs_frontend(state)
    assert not _frontend(state).exists()
    assert not (state / ".hacs_frontend.lock").exists()


def test_existing_javascript_without_python_module_is_repaired(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = _frontend(state)
    frontend.mkdir()
    (frontend / "entrypoint.js").write_text("export {};", encoding="utf-8")

    def download(target: Path) -> None:
        (target / "__init__.py").write_text("VERSION = 'fixture'", encoding="utf-8")

    monkeypatch.setattr(seed, "_download_hacs_frontend", download)
    seed._ensure_hacs_frontend(state)
    assert (frontend / "__init__.py").is_file()


def test_waiter_refuses_missing_frontend_after_owner_releases_lock(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (state / ".hacs_frontend.lock").mkdir()
    monkeypatch.setattr(seed.time, "time", lambda: 0.0)
    monkeypatch.setattr(seed, "_wait_for_hacs_lock_release", lambda path: path.rmdir())
    with pytest.raises(RuntimeError, match="HACS frontend"):
        seed._ensure_hacs_frontend(state)


def test_complete_archive_provides_importable_frontend_and_releases_lock(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve_archive(
        seed,
        monkeypatch,
        _archive(
            {
                "hacs_frontend/__init__.py": b"VERSION = 'fixture'",
                "hacs_frontend/entrypoint.js": b"export {};",
            }
        ),
    )
    seed._ensure_hacs_frontend(state)
    module_path = _frontend(state) / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "qualified_hacs_frontend", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.VERSION == "fixture"
    assert not (state / ".hacs_frontend.lock").exists()


def test_absent_hacs_component_needs_no_frontend(
    seed: ModuleType, tmp_path: Path
) -> None:
    seed._ensure_hacs_frontend(tmp_path)
    assert not (tmp_path / ".hacs_frontend.lock").exists()


def test_complete_frontend_without_lock_needs_no_download(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = _frontend(state)
    frontend.mkdir()
    (frontend / "__init__.py").write_text("VERSION = 'fixture'", encoding="utf-8")
    (frontend / "entrypoint.js").write_text("export {};", encoding="utf-8")

    def unexpected_download(target: Path) -> NoReturn:
        pytest.fail("A complete published frontend must not be downloaded again")

    monkeypatch.setattr(seed, "_download_hacs_frontend", unexpected_download)
    seed._ensure_hacs_frontend(state)


def test_active_lock_times_out_even_with_both_sentinels_and_stays_owned(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = _frontend(state)
    frontend.mkdir()
    (frontend / "__init__.py").write_text("VERSION = 'fixture'", encoding="utf-8")
    (frontend / "entrypoint.js").write_text("export {};", encoding="utf-8")
    lock = state / ".hacs_frontend.lock"
    lock.mkdir()
    lock_created = lock.stat().st_mtime
    elapsed = 0.0

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    with monkeypatch.context() as clock:
        clock.setattr(seed.time, "time", lambda: lock_created)
        clock.setattr(seed.time, "monotonic", lambda: elapsed)
        clock.setattr(seed.time, "sleep", advance)
        with pytest.raises(RuntimeError, match="HACS frontend lock"):
            seed._ensure_hacs_frontend(state)

    assert lock.is_dir()


def test_waiter_uses_frontend_only_after_owner_publishes_and_releases(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = state / ".hacs_frontend.lock"
    lock.mkdir()
    lock_created = lock.stat().st_mtime
    waited = False

    def publish_and_release(seconds: float) -> None:
        nonlocal waited
        waited = True
        frontend = _frontend(state)
        frontend.mkdir()
        (frontend / "__init__.py").write_text("VERSION = 'fixture'", encoding="utf-8")
        (frontend / "entrypoint.js").write_text("export {};", encoding="utf-8")
        lock.rmdir()

    def unexpected_download(target: Path) -> NoReturn:
        pytest.fail("A waiter must use the frontend published by the owner")

    monkeypatch.setattr(seed, "_download_hacs_frontend", unexpected_download)
    with monkeypatch.context() as clock:
        clock.setattr(seed.time, "time", lambda: lock_created)
        clock.setattr(seed.time, "monotonic", lambda: 0.0)
        clock.setattr(seed.time, "sleep", publish_and_release)
        seed._ensure_hacs_frontend(state)

    assert waited
    assert not lock.exists()
    assert (_frontend(state) / "__init__.py").is_file()
