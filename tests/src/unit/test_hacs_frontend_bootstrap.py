"""Refuse an unusable HACS frontend before starting test containers."""

from __future__ import annotations

import errno
import importlib.util
import io
import tarfile
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable
from http.client import HTTPMessage
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

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
            member = tarfile.TarInfo(f"hacs_frontend-20250128065759/{name}")
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
    responses = iter([payload])
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
        (_archive({"hacs_frontend/__init__.py": b""}), RuntimeError),
    ],
    ids=[
        "corrupt-archive",
        "missing-package",
        "missing-python-module",
        "missing-browser-entry",
    ],
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

    (frontend / "obsolete.js").write_text("old", encoding="utf-8")
    _serve_archive(
        seed,
        monkeypatch,
        _archive(
            {"hacs_frontend/__init__.py": b"", "hacs_frontend/entrypoint.js": b""}
        ),
    )
    seed._ensure_hacs_frontend(state)
    assert (frontend / "__init__.py").is_file()
    assert not (frontend / "obsolete.js").exists()


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
    assert not list(state.glob(".hacs_frontend-staging-*"))


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

    def cross_filesystem(src: Any, dst: Any) -> None:
        if Path(dst) == frontend:
            assert (state / ".hacs_frontend.lock").is_dir()
            raise OSError(errno.EXDEV, "fixture cross-filesystem rename")
        real_rename(src, dst)

    with monkeypatch.context() as publication:
        publication.setattr(seed.os, "rename", cross_filesystem)
        with pytest.raises(OSError):
            seed._ensure_hacs_frontend(state)

    assert not frontend.exists()
    assert not (state / ".hacs_frontend.lock").exists()
    assert list(frontend.parent.iterdir()) == []
    assert not list(state.glob(".hacs_frontend-staging-*"))
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
    assert not list(state.glob(".hacs_frontend-staging-*"))
    assert not (state / ".hacs_frontend.lock").exists()


@pytest.mark.parametrize("version", ["2026.11.0b0", "2026.11.0", "2027.1.0"])
def test_marketplace_core_never_downloads_legacy_hacs(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    def unexpected(target: Path) -> NoReturn:
        pytest.fail("Core blocks legacy HACS; no frontend download is needed")

    monkeypatch.setattr(seed, "_download_hacs_frontend", unexpected)
    seed._ensure_hacs_frontend(state, core_version=version)
    assert not (state / ".hacs_frontend.lock").exists()


def test_stale_owner_recovers_and_sweeps_abandoned_staging(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = state / ".hacs_frontend.lock"
    lock.mkdir()
    created = lock.stat().st_mtime
    monkeypatch.setattr(seed.time, "time", lambda: created + 301)
    abandoned = state / ".hacs_frontend-staging-orphan"
    abandoned.mkdir()
    (abandoned / "partial").write_text("incomplete", encoding="utf-8")
    unrelated = state / "keep-me"
    unrelated.mkdir()
    _serve_archive(
        seed,
        monkeypatch,
        _archive(
            {"hacs_frontend/__init__.py": b"", "hacs_frontend/entrypoint.js": b""}
        ),
    )
    seed._ensure_hacs_frontend(state)
    assert not abandoned.exists()
    assert unrelated.is_dir()
    assert not lock.exists()
    assert (_frontend(state) / "entrypoint.js").is_file()


def test_slow_owner_progress_keeps_waiters_alive_beyond_initial_budget(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = state / ".hacs_frontend.lock"
    lock.mkdir()
    elapsed = 0.0
    created = lock.stat().st_mtime

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds
        seed.os.utime(lock, (created + elapsed, created + elapsed))
        if elapsed >= 660:
            frontend = _frontend(state)
            frontend.mkdir()
            (frontend / "__init__.py").write_text("", encoding="utf-8")
            (frontend / "entrypoint.js").write_text("", encoding="utf-8")
            lock.rmdir()

    monkeypatch.setattr(seed.time, "time", lambda: created)
    monkeypatch.setattr(seed.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(seed.time, "sleep", advance)
    seed._ensure_hacs_frontend(state)
    assert not lock.exists()


def test_archive_fetch_is_pinned_and_anonymous_even_with_local_tokens(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "unused-fixture-token")
    monkeypatch.setenv("HACS_FRONTEND_GITHUB_TOKEN", "unused-fixture-alias")
    requests: list[urllib.request.Request] = []
    payload = _archive(
        {"hacs_frontend/__init__.py": b"", "hacs_frontend/entrypoint.js": b""}
    )

    def respond(request: urllib.request.Request) -> urllib.response.addinfourl:
        requests.append(request)
        return _http_response(request, payload)

    _stub_http(seed, monkeypatch, respond)
    seed._ensure_hacs_frontend(state, core_version="2026.10.4")
    assert [r.full_url for r in requests] == [
        "https://github.com/hacs/frontend/releases/download/20250128065759/hacs_frontend-20250128065759.tar.gz"
    ]
    assert requests[0].get_header("Authorization") is None


def test_live_download_renews_lock_and_stops_heartbeat_on_failure(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    refreshed = threading.Event()
    lock = state / ".hacs_frontend.lock"
    real_utime = seed.os.utime
    threads = []
    renewals = 0

    def make_thread(*args: Any, **kwargs: Any) -> threading.Thread:
        thread = threading.Thread(*args, **kwargs)
        threads.append(thread)
        return thread

    def renew(path: Path, times: Any = None) -> None:
        nonlocal renewals
        real_utime(path, (400.0, 400.0))
        renewals += 1
        if renewals >= 3:
            refreshed.set()

    def download(target: Path, **kwargs: Any) -> NoReturn:
        assert refreshed.wait(2), "Live owner never renewed its lock"
        seed._clear_stale_hacs_lock(lock)
        assert lock.is_dir()
        raise OSError("fixture owner failure")

    monkeypatch.setattr(seed, "_HACS_HEARTBEAT_INTERVAL_S", 0.01, raising=False)
    monkeypatch.setattr(seed, "Thread", make_thread, raising=False)
    monkeypatch.setattr(seed.os, "utime", renew)
    monkeypatch.setattr(seed.time, "time", lambda: 401.0)
    monkeypatch.setattr(seed, "_download_hacs_frontend", download)
    with pytest.raises(OSError, match="fixture owner failure"):
        seed._ensure_hacs_frontend(state)
    assert threads and all(not t.is_alive() for t in threads)
    assert not lock.exists()


@pytest.mark.parametrize(
    ("image", "probed_version"),
    [
        ("ghcr.io/home-assistant/home-assistant:stable", "2026.11.0"),
        ("homeassistant/home-assistant:latest", "2026.10.4"),
    ],
)
def test_symbolic_core_version_comes_from_image_without_starting_ha(
    image: str, probed_version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    import docker

    from tests.src.e2e import _conftest_testcontainer as containers

    client = MagicMock()
    client.containers.run.return_value = f"{probed_version}\n".encode()
    client.images.get.return_value.id = "sha256:fixture-immutable"
    monkeypatch.setattr(docker, "from_env", lambda: client)
    initial = tmp_path / "initial_test_state"
    initial.mkdir()
    monkeypatch.setattr(containers, "HA_TEST_IMAGE", image)
    monkeypatch.setattr(
        containers, "__file__", str(tmp_path / "src" / "e2e" / "seed.py")
    )
    monkeypatch.setattr(containers.tempfile, "mkdtemp", lambda **kw: str(tmp_path))

    def bootstrap(path: Path, *, core_version: str) -> None:
        assert path == initial
        assert core_version == probed_version

    monkeypatch.setattr(containers, "_ensure_hacs_frontend", bootstrap)
    monkeypatch.setattr(
        containers, "_refresh_recorder_timestamps", lambda *a, **kw: None
    )
    monkeypatch.setattr(containers, "_copy_local_blueprint_to_www", lambda *a, **kw: {})
    *_, resolved_image = containers._prepare_testcontainer_config(embedded=False)
    assert resolved_image == "sha256:fixture-immutable"
    args, kwargs = client.containers.run.call_args
    assert args[0] == "sha256:fixture-immutable"
    assert kwargs["network_disabled"] and kwargs["remove"]
    assert kwargs["entrypoint"][0] == "python"


def test_numeric_image_version_needs_no_docker_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import docker

    from tests.src.e2e import _conftest_testcontainer as containers

    def unexpected() -> NoReturn:
        pytest.fail("Numeric Core tags need no Docker version probe")

    monkeypatch.setattr(docker, "from_env", unexpected)
    assert containers._resolve_core_image(
        "ghcr.io/home-assistant/home-assistant:2026.11.0b0"
    ) == ("ghcr.io/home-assistant/home-assistant:2026.11.0b0", "2026.11.0b0")


def test_failed_lock_renewal_refuses_publication(
    seed: ModuleType, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    failed = threading.Event()
    real_extract = tarfile.TarFile.extractall
    real_utime = seed.os.utime
    real_error = seed.logger.error
    lock = state / ".hacs_frontend.lock"

    def denied(path: Path, *args: Any, **kwargs: Any) -> None:
        if Path(path) == lock:
            raise PermissionError("fixture renewal denied")
        real_utime(path, *args, **kwargs)

    def report_failure(*args: Any, **kwargs: Any) -> None:
        real_error(*args, **kwargs)
        failed.set()

    def extract(archive: tarfile.TarFile, *args: Any, **kwargs: Any) -> None:
        real_extract(archive, *args, **kwargs)
        assert failed.wait(2), "The renewal fault was never injected"

    _serve_archive(
        seed,
        monkeypatch,
        _archive(
            {"hacs_frontend/__init__.py": b"", "hacs_frontend/entrypoint.js": b""}
        ),
    )
    monkeypatch.setattr(seed, "_HACS_HEARTBEAT_INTERVAL_S", 0.01)
    monkeypatch.setattr(seed.os, "utime", denied)
    monkeypatch.setattr(seed.logger, "error", report_failure)
    monkeypatch.setattr(tarfile.TarFile, "extractall", extract)
    unrelated = state / "archive-member"
    unrelated.write_bytes(b"fixture")
    seed.os.utime(unrelated, None)
    assert not failed.is_set()
    with pytest.raises(RuntimeError, match="renewal failed"):
        seed._ensure_hacs_frontend(state)
    assert not _frontend(state).exists()
    assert not (state / ".hacs_frontend.lock").exists()
    assert not list(state.glob(".hacs_frontend-staging-*"))


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("sha256:fixture-immutable", "sha256:fixture-immutable"),
        (None, "homeassistant/home-assistant:stable"),
    ],
)
def test_testcontainer_preserves_resolved_and_default_image_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image: str | None, expected: str
) -> None:
    from tests.src.e2e import _conftest_testcontainer as containers

    class ConstructionReached(Exception):
        pass

    def construct(image: str) -> NoReturn:
        assert image == expected
        raise ConstructionReached

    monkeypatch.setattr(
        containers, "HA_TEST_IMAGE", "homeassistant/home-assistant:stable"
    )
    monkeypatch.setattr(containers, "DockerContainer", construct)
    with pytest.raises(ConstructionReached):
        kwargs = {} if image is None else {"image": image}
        containers._build_ha_testcontainer(tmp_path, False, None, **kwargs)


@pytest.mark.parametrize("failure", ["probe", "bootstrap", "copy"])
def test_failed_config_preparation_does_not_leak_session_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from tests.src.e2e import _conftest_testcontainer as containers

    initial = tmp_path / "initial_test_state"
    initial.mkdir()
    monkeypatch.setattr(
        containers, "__file__", str(tmp_path / "src" / "e2e" / "seed.py")
    )
    allocated: list[Path] = []
    real_mkdtemp = containers.tempfile.mkdtemp

    def allocate(**kwargs: Any) -> str:
        path = real_mkdtemp(dir=tmp_path, **kwargs)
        allocated.append(Path(path))
        return path

    def fail(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("fixture preparation failure")

    monkeypatch.setattr(containers.tempfile, "mkdtemp", allocate)
    monkeypatch.setattr(
        containers,
        "_resolve_core_image",
        fail if failure == "probe" else lambda image: (image, "2026.10.4"),
    )
    monkeypatch.setattr(
        containers,
        "_ensure_hacs_frontend",
        fail if failure == "bootstrap" else lambda *a, **kw: None,
    )
    if failure == "copy":
        monkeypatch.setattr(containers.shutil, "copytree", fail)
    with pytest.raises(RuntimeError, match="fixture preparation failure"):
        containers._prepare_testcontainer_config(embedded=False)
    if failure == "copy":
        assert allocated
    else:
        assert not allocated
    assert all(not path.exists() for path in allocated)
    assert initial.is_dir()


def test_e2e_fixture_forwards_the_prepared_immutable_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inspect import unwrap
    from unittest.mock import MagicMock

    import docker

    from tests.src.e2e import conftest as e2e

    class BuilderReached(Exception):
        pass

    resolved = "sha256:fixture-prepared-image"

    def build(
        config_path: Path,
        embedded: bool,
        wheel: str | None,
        *,
        image: str | None = None,
    ) -> NoReturn:
        assert image == resolved
        raise BuilderReached

    monkeypatch.setattr(e2e, "is_haos_backend_selected", lambda: False)
    monkeypatch.setattr(e2e, "_is_embedded_backend_selected", lambda: False)
    monkeypatch.setattr(
        e2e,
        "_prepare_testcontainer_config",
        lambda embedded: (tmp_path, str(tmp_path), {}, None, resolved),
    )
    monkeypatch.setattr(e2e, "_build_ha_testcontainer", build)
    monkeypatch.setattr(docker, "from_env", MagicMock())
    fixture = unwrap(e2e.ha_container_with_fresh_config)(MagicMock(), tmp_path)
    with pytest.raises(BuilderReached):
        next(fixture)


@pytest.mark.parametrize("phase", ["build", "enter", "readiness", "success"])
def test_fixture_releases_prepared_config_after_setup_or_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from inspect import unwrap
    from unittest.mock import MagicMock

    import docker

    from tests.src.e2e import conftest as e2e

    owned = tmp_path / "session"
    owned.mkdir()
    preserved = tmp_path / "unrelated"
    preserved.mkdir()
    finalizers: list[Callable[[], None]] = []
    request = MagicMock()
    request.config.stash = {}
    request.addfinalizer.side_effect = finalizers.append
    container = MagicMock()
    container.__enter__.return_value = container
    container.__exit__.return_value = False
    container.get_exposed_port.return_value = 8123
    container.get_container_host_ip.return_value = "127.0.0.1"

    def fail(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("fixture setup failure")

    if phase == "enter":
        container.__enter__.side_effect = fail
    monkeypatch.setattr(e2e.os, "environ", e2e.os.environ.copy())
    monkeypatch.setattr(docker, "from_env", MagicMock())
    monkeypatch.setattr(e2e, "is_haos_backend_selected", lambda: False)
    monkeypatch.setattr(e2e, "_is_embedded_backend_selected", lambda: False)
    monkeypatch.setattr(
        e2e,
        "_prepare_testcontainer_config",
        lambda embedded: (owned, str(owned), {}, None, "sha256:fixture"),
    )
    monkeypatch.setattr(
        e2e,
        "_build_ha_testcontainer",
        fail if phase == "build" else lambda *a, **kw: container,
    )
    monkeypatch.setattr(e2e, "_reset_ha_in_process_caches", lambda: None)
    monkeypatch.setattr(
        e2e,
        "_wait_for_testcontainer_ready",
        fail if phase == "readiness" else lambda *a, **kw: None,
    )
    fixture = unwrap(e2e.ha_container_with_fresh_config)(request, tmp_path)
    try:
        if phase == "success":
            next(fixture)
            fixture.close()
        else:
            with pytest.raises(RuntimeError, match="fixture setup failure"):
                next(fixture)
    finally:
        for cleanup in reversed(finalizers):
            cleanup()
    assert not owned.exists()
    assert preserved.is_dir()
