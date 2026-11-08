"""Run pytest in a subprocess on a scratch directory, with plugin autoloading
off, so third-party plugins load only through ``-p``, and no conftest above
that directory."""

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def run_pytest(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.pop("PYTEST_ADDOPTS", None)
    run_env.pop("PYTEST_TIMEOUT", None)
    run_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # The scratch conftest and test modules import repo code.
    run_env["PYTHONPATH"] = os.pathsep.join((str(_REPO_ROOT), str(_REPO_ROOT / "src")))
    run_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "--confcutdir", str(tmp_path)],
        cwd=tmp_path,
        env=run_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
