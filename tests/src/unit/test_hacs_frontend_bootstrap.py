"""Refuse an unusable HACS frontend before starting test containers."""

from __future__ import annotations

import errno
import importlib.util
import io
import json
import logging
import os
import tarfile
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable
from http.client import HTTPMessage
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest

_REAL_BUILD_OPENER = urllib.request.build_opener


@pytest.fixture(autouse=True)
def isolated_github_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("HACS_FRONTEND_GITHUB_TOKEN", raising=False)


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


class _HTTPResponse(urllib.response.addinfourl):
    msg: str


def _http_response(
    request: urllib.request.Request,
    payload: bytes,
    status: int = 200,
    location: str | None = None,
) -> _HTTPResponse:
    headers = HTTPMessage()
    if location:
        headers["Location"] = location
    response = _HTTPResponse(io.BytesIO(payload), headers, request.full_url, status)
    response.msg = "rate limit exceeded" if status == 403 else "fixture response"
    return response


def _stub_http(
    seed: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[urllib.request.Request], urllib.response.addinfourl],
) -> None:
    class HTTPTransport(urllib.request.HTTPHandler):
        def http_open(self, request: urllib.request.Request) -> Any:
            return respond(request)

    class HTTPSTransport(urllib.request.HTTPSHandler):
        def https_open(self, request: urllib.request.Request) -> Any:
            return respond(request)

    def build_opener(*handlers: Any) -> urllib.request.OpenerDirector:
        return _REAL_BUILD_OPENER(
            urllib.request.ProxyHandler({}),
            HTTPTransport(),
            HTTPSTransport(),
            *handlers,
        )

    monkeypatch.setattr(seed.urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(seed.urllib.request, "urlopen", build_opener().open)


def _serve_archive(
    seed: ModuleType, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    responses = iter([json.dumps({"tag_name": "1.0.0"}).encode(), payload])
    _stub_http(
        seed,
        monkeypatch,
        lambda request: _http_response(request, next(responses)),
    )


def test_download_failure_aborts_bootstrap_and_releases_lock(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(request: urllib.request.Request) -> NoReturn:
        raise urllib.error.URLError("fixture network unavailable")

    _stub_http(seed, monkeypatch, unavailable)
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
    assert list(_frontend(state).parent.iterdir()) == [_frontend(state)]


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


def test_failed_cross_filesystem_publication_leaves_no_frontend_to_accept(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = _frontend(state)
    payload = _archive(
        {
            "hacs_frontend/__init__.py": b"VERSION = 'fixture'",
            "hacs_frontend/entrypoint.js": b"import './z-asset.js';",
            "hacs_frontend/z-asset.js": b"export {};",
        }
    )
    _serve_archive(seed, monkeypatch, payload)
    real_rename = seed.os.rename
    real_scandir = seed.os.scandir
    real_copyfile = seed.shutil.copyfile
    real_move = seed.shutil.move

    def cross_filesystem(src: Any, dst: Any) -> None:
        if Path(dst) == frontend:
            assert (state / ".hacs_frontend.lock").is_dir()
            raise OSError(errno.EXDEV, "fixture cross-filesystem rename")
        real_rename(src, dst)

    def ordered_scandir(path: Any) -> MagicMock:
        with real_scandir(path) as entries:
            ordered = iter(sorted(entries, key=lambda entry: entry.name))
        scan = MagicMock()
        scan.__enter__.return_value = ordered
        scan.__iter__.side_effect = lambda: ordered
        scan.__next__.side_effect = ordered.__next__
        return scan

    def copy_asset(src: Any, dst: Any) -> str:
        if Path(src).name == "z-asset.js":
            assert (frontend / "__init__.py").is_file()
            assert (frontend / "entrypoint.js").is_file()
            raise OSError("fixture asset copy failed")
        return str(real_copyfile(src, dst))

    def move(src: str, dst: str) -> str:
        return str(real_move(src, dst, copy_function=copy_asset))

    with monkeypatch.context() as publication:
        publication.setattr(seed.os, "rename", cross_filesystem)
        publication.setattr(seed.os, "scandir", ordered_scandir)
        publication.setattr(seed.shutil, "move", move)
        with pytest.raises(OSError):
            seed._ensure_hacs_frontend(state)

    assert not frontend.exists()
    assert not (state / ".hacs_frontend.lock").exists()
    assert list(frontend.parent.iterdir()) == []
    _serve_archive(seed, monkeypatch, payload)
    seed._ensure_hacs_frontend(state)
    assert (frontend / "z-asset.js").read_bytes() == b"export {};"


def test_failed_rename_preserves_concurrent_destination_and_cleans_staging(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = _frontend(state)
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
    real_rename = seed.os.rename

    def competing_publication(src: Any, dst: Any) -> None:
        if Path(dst) == frontend:
            assert (state / ".hacs_frontend.lock").is_dir()
            frontend.mkdir()
            (frontend / "other-owner.txt").write_text("preserve me", encoding="utf-8")
        real_rename(src, dst)

    monkeypatch.setattr(seed.os, "rename", competing_publication)
    with pytest.raises(OSError):
        seed._ensure_hacs_frontend(state)

    assert (frontend / "other-owner.txt").read_text(encoding="utf-8") == "preserve me"
    assert list(frontend.iterdir()) == [frontend / "other-owner.txt"]
    assert list(frontend.parent.iterdir()) == [frontend]
    assert not (state / ".hacs_frontend.lock").exists()


@pytest.mark.parametrize(
    ("credential", "github_fallback"),
    [
        ("GITHUB_TOKEN", False),
        ("HACS_FRONTEND_GITHUB_TOKEN", False),
        ("HACS_FRONTEND_GITHUB_TOKEN", True),
    ],
    ids=["job-token", "metadata-alias-only", "metadata-alias-priority"],
)
def test_token_authenticates_only_fixed_metadata_request(
    seed: ModuleType,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    credential: str,
    github_fallback: bool,
) -> None:
    token = "fixture-metadata-token"
    caplog.set_level(logging.DEBUG, logger=seed.__name__)
    if github_fallback:
        monkeypatch.setenv("GITHUB_TOKEN", "fixture-fallback-token")
    monkeypatch.setenv(credential, token)
    github_token_before = os.environ.get("GITHUB_TOKEN")
    metadata_url = "https://api.github.com/repos/hacs/frontend/releases/latest"
    asset_url = "https://github.com/hacs/frontend/releases/download/1.0.0/hacs_frontend-1.0.0.tar.gz"
    download_url = (
        "https://release-assets.githubusercontent.com/fixture/frontend.tar.gz"
    )
    requests: list[urllib.request.Request] = []
    payload = _archive(
        {
            "hacs_frontend/__init__.py": b"VERSION = 'fixture'",
            "hacs_frontend/entrypoint.js": b"export {};",
        }
    )

    def respond(request: urllib.request.Request) -> urllib.response.addinfourl:
        requests.append(request)
        if request.full_url == metadata_url:
            if request.get_header("Authorization") != f"Bearer {token}":
                return _http_response(request, b"rate limit exceeded", 403)
            return _http_response(request, b'{"tag_name":"1.0.0"}')
        if request.full_url == asset_url:
            return _http_response(request, b"", 302, download_url)
        assert request.full_url == download_url
        return _http_response(request, payload)

    _stub_http(seed, monkeypatch, respond)
    seed._ensure_hacs_frontend(state)

    assert os.environ.get("GITHUB_TOKEN") == github_token_before
    assert [request.full_url for request in requests] == [
        metadata_url,
        asset_url,
        download_url,
    ]
    assert [request.get_header("Authorization") for request in requests] == [
        f"Bearer {token}",
        None,
        None,
    ]
    redirected = urllib.request.HTTPRedirectHandler().redirect_request(
        requests[0],
        io.BytesIO(),
        302,
        "Found",
        HTTPMessage(),
        "https://redirect.invalid/",
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None
    assert token not in caplog.text
    assert "fixture-fallback-token" not in caplog.text
    for path in state.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes()
            assert b"fixture-fallback-token" not in path.read_bytes()


def test_metadata_redirect_is_refused_before_any_other_host_is_contacted(
    seed: ModuleType,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "fixture-metadata-token"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    caplog.set_level(logging.DEBUG, logger=seed.__name__)
    metadata_url = "https://api.github.com/repos/hacs/frontend/releases/latest"
    requests: list[str] = []

    def respond(request: urllib.request.Request) -> urllib.response.addinfourl:
        requests.append(request.full_url)
        if request.full_url == metadata_url:
            return _http_response(request, b"", 302, "https://redirect.invalid/release")
        pytest.fail("The metadata redirect was followed")

    _stub_http(seed, monkeypatch, respond)
    with pytest.raises(urllib.error.HTTPError) as error:
        seed._ensure_hacs_frontend(state)
    assert error.value.code == 302
    assert requests == [metadata_url]
    assert token not in caplog.text
    assert not _frontend(state).exists()
    assert not (state / ".hacs_frontend.lock").exists()


def test_local_metadata_lookup_without_a_token_remains_anonymous(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[urllib.request.Request] = []
    responses = iter(
        [
            b'{"tag_name":"1.0.0"}',
            _archive(
                {
                    "hacs_frontend/__init__.py": b"VERSION = 'fixture'",
                    "hacs_frontend/entrypoint.js": b"export {};",
                }
            ),
        ]
    )

    def respond(request: urllib.request.Request) -> urllib.response.addinfourl:
        requests.append(request)
        return _http_response(request, next(responses))

    _stub_http(seed, monkeypatch, respond)
    seed._ensure_hacs_frontend(state)
    assert len(requests) == 2
    assert all(request.get_header("Authorization") is None for request in requests)


def test_authenticated_metadata_failure_is_not_retried_or_logged_with_token(
    seed: ModuleType,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "fixture-sensitive-token"
    caplog.set_level(logging.DEBUG, logger=seed.__name__)
    monkeypatch.setenv("GITHUB_TOKEN", token)
    requests: list[str] = []

    def respond(request: urllib.request.Request) -> urllib.response.addinfourl:
        requests.append(request.full_url)
        return _http_response(request, b"rate limit exceeded", 403)

    _stub_http(seed, monkeypatch, respond)
    with pytest.raises(urllib.error.HTTPError) as error:
        seed._ensure_hacs_frontend(state)
    assert error.value.code == 403
    assert requests == ["https://api.github.com/repos/hacs/frontend/releases/latest"]
    assert token not in caplog.text
    assert not _frontend(state).exists()
    assert not (state / ".hacs_frontend.lock").exists()
