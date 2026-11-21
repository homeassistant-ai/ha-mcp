"""No directory joins ``tests/`` where CI would ignore it.

CI runs four trees in full, so a directory added inside one of them is picked
up. Anywhere else, CI names single files or does not look at all.
"""

import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).parents[3]
# The trees a CI command runs recursively: `pytest tests/src/unit/`,
# `pytest tests/addon/`, `pytest src/e2e/` and `pytest tests/uat/`.
RUN_IN_FULL = ("tests/src/unit", "tests/src/e2e", "tests/addon", "tests/uat")
ELSEWHERE = {
    "tests",
    "tests/assets",
    "tests/assets/blueprints",
    "tests/haos_image_build",
    "tests/haos_image_build/screenshot_engine_mock",
    "tests/initial_test_state",
    "tests/initial_test_state/.storage",
    "tests/initial_test_state/blueprints",
    "tests/initial_test_state/blueprints/automation",
    "tests/initial_test_state/blueprints/automation/homeassistant",
    "tests/initial_test_state/blueprints/script",
    "tests/initial_test_state/blueprints/script/homeassistant",
    "tests/initial_test_state/custom_packages",
    "tests/initial_test_state/custom_packages/nested",
    "tests/initial_test_state/themes",
    "tests/initial_test_state/www",
    "tests/js",
    "tests/lab-setup",
    "tests/renovate",
    "tests/src",
    "tests/test-env",
    "tests/test-env/src",
    "tests/test-env/src/hamcp_test_env",
    "tests/test_docker",
    "tests/zwave_mock",
}


def _tracked_directories() -> set[str]:
    files = subprocess.run(
        # CI's container runs as another uid than the checkout's owner, which
        # git refuses as dubious ownership.
        ["git", "-c", f"safe.directory={REPOSITORY}", "ls-files", "-z", "--", "tests"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    directories: set[str] = set()
    for path in filter(None, files):
        parts = path.split("/")[:-1]
        directories.update(
            "/".join(parts[:depth]) for depth in range(1, len(parts) + 1)
        )
    return directories


def test_no_new_directory_is_outside_what_ci_runs() -> None:
    """Tests in a directory no CI command covers never run and are never checked.

    Put a new directory inside a tree in RUN_IN_FULL; test code that drives Home
    Assistant Core goes in ``src/unit`` or ``addon``, which the real-Core checks
    scan. A directory that has to live elsewhere is added to ELSEWHERE in the
    change that creates it, together with the CI command that covers it.
    """
    outside = {
        directory
        for directory in _tracked_directories()
        if not any(
            directory == tree or directory.startswith(f"{tree}/")
            for tree in RUN_IN_FULL
        )
    }
    assert outside - ELSEWHERE == set()
