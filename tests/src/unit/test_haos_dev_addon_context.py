"""The HAOS lanes build the dev app from a staged copy of the repo.

Supervisor builds the image with the app directory as the build context, so
every repo file the dev Dockerfile copies must be staged into it. A file left
out fails the build inside HAOS, which the lanes only see as an "unknown
error" from Supervisor.
"""

from __future__ import annotations

import importlib
import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

from tests.src import haos_runtime

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Copied as a tree by its own step.
_STAGED_SEPARATELY = {"src"}


def _repo_files_the_dockerfile_copies() -> set[str]:
    dockerfile = _REPO_ROOT / "homeassistant-addon-dev" / "Dockerfile"
    sources: set[str] = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words or words[0] != "COPY" or any("--from=" in w for w in words):
            continue
        sources.update(word.rstrip("/") for word in words[1:-1])
    return sources - _STAGED_SEPARATELY


@pytest.mark.parametrize(
    "module_name",
    ["dev_addon_tree", "haos_runtime"],
    ids=["image bake", "per-PR refresh"],
)
def test_the_staged_dev_app_context_holds_every_file_the_dockerfile_copies(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "tests" / "haos_image_build"))
    module: types.ModuleType = (
        haos_runtime
        if module_name == "haos_runtime"
        else importlib.import_module(module_name)
    )
    expected = _repo_files_the_dockerfile_copies()
    assert expected, "found no COPY sources in the dev Dockerfile"
    staged = set(module.DEV_ADDON_REPO_FILES) | {
        f"homeassistant-addon/{name}" for name in module.STABLE_ADDON_FILES
    }

    assert sorted(expected - staged) == []


def _load_dev_env_holder(monkeypatch: pytest.MonkeyPatch) -> object:
    """Import ``.github/dev-ha-env/hold.py`` as the workflow runs it.

    The workflow copies the file into ``tests/src/e2e/``, so it resolves the
    repo root from that location and uses that package's relative imports;
    compiling it under that path reproduces both without copying.
    """
    from tests.src.e2e._conftest_embedded import _EMBEDDED_FEATURE_FLAGS as flags

    source_path = _REPO_ROOT / ".github" / "dev-ha-env" / "hold.py"
    run_path = _REPO_ROOT / "tests" / "src" / "e2e" / "hold.py"
    # The holder edits the shared flags at import; restore them afterwards.
    monkeypatch.setitem(
        flags, "enable_strict_mandatory_bps", flags["enable_strict_mandatory_bps"]
    )
    monkeypatch.setitem(flags, "enable_dev_mode", False)
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "tests" / "src"))
    module = types.ModuleType("tests.src.e2e._dev_env_holder")
    module.__file__ = str(run_path)
    module.__package__ = "tests.src.e2e"
    exec(
        compile(source_path.read_text(encoding="utf-8"), str(run_path), "exec"),
        module.__dict__,
    )
    return module


def test_the_dev_env_runs_its_embedded_server_in_developer_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/dev-ha-env.md updates the embedded server with ha_dev_manage_server,
    which only a server in developer mode exposes."""
    from tests.src.e2e._conftest_embedded import _EMBEDDED_FEATURE_FLAGS as flags

    _load_dev_env_holder(monkeypatch)

    assert flags["enable_dev_mode"] is True


class _SwitchedInstance:
    """Stands in for the holder's Instance: records which commits it ran."""

    backend = "container"

    def __init__(self, failing: dict[str, Exception]) -> None:
        self.failing = failing
        self.ran: list[str] = []
        self.ha_messages: list[tuple[str, str]] = []

    def run_commit(self, sha: str) -> None:
        self.ran.append(sha)
        if sha in self.failing:
            raise self.failing[sha]

    def ha_log(self, level: str, message: str) -> None:
        self.ha_messages.append((level, message))


_START, _NEXT, _LATER = "a" * 40, "b" * 40, "c" * 40


def test_a_standalone_switch_that_fails_runs_the_previous_commit_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: Any = _load_dev_env_holder(monkeypatch)
    sync_failed = subprocess.CalledProcessError(1, ["uv", "sync"])
    inst = _SwitchedInstance({_NEXT: sync_failed})

    assert holder.switch_server(inst, _START, _NEXT) == _START

    assert inst.ran == [_NEXT, _START]
    level, message = inst.ha_messages[-1]
    assert level == "error"
    assert "CalledProcessError" in message
    assert f"runs {_START} again" in message


def test_a_commit_that_does_not_start_reports_the_end_of_the_server_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: Any = _load_dev_env_holder(monkeypatch)
    monkeypatch.setattr(holder, "server_log_tail", lambda: "ImportError: boom")
    no_answer = RuntimeError("standalone server did not answer")
    inst = _SwitchedInstance({_NEXT: no_answer, _START: no_answer})

    assert holder.switch_server(inst, _START, _NEXT) == _START

    level, message = inst.ha_messages[-1]
    assert level == "error"
    assert message.count("ImportError: boom") == 1
    assert f"nor go back to {_START}" in message


def test_the_holder_follows_the_ref_the_docs_tell_agents_to_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/dev-ha-env.md, step 5: each commit pushed to refs/dev-ha-env/<run-id>
    is tried once, also when it fails."""
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    holder: Any = _load_dev_env_holder(monkeypatch)
    monkeypatch.setattr(holder, "SWITCH_POLL_SECONDS", 0)
    docs = (_REPO_ROOT / "docs" / "dev-ha-env.md").read_text(encoding="utf-8")
    assert ":refs/dev-ha-env/<run-id>" in docs
    assert holder.SWITCH_REF == "refs/dev-ha-env/4242"

    class _Polled(Exception):
        pass

    polls = iter([_START, "", _NEXT, _NEXT, _LATER, _LATER])

    def ls_remote(*args: str) -> str:
        assert args == ("ls-remote", "origin", "refs/dev-ha-env/4242")
        sha = next(polls, None)
        if sha is None:
            raise _Polled
        return f"{sha}\trefs/dev-ha-env/4242" if sha else ""

    monkeypatch.setattr(holder, "git", ls_remote)
    switches: list[tuple[str, str]] = []

    def switch(inst: object, current: str, wanted: str) -> str:
        switches.append((current, wanted))
        return current if wanted == _LATER else wanted

    monkeypatch.setattr(holder, "switch_server", switch)

    with pytest.raises(_Polled):
        holder.follow_switch_ref(object(), _START, float("inf"))

    assert switches == [(_START, _NEXT), (_NEXT, _LATER)]
