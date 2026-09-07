"""Exercise release metadata and image inputs without starting a VM or network."""

import json
import os
import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_BUILDER = _ROOT / "tests/haos_image_build/build_image.py"


@pytest.mark.parametrize("beta", [False, True])
def test_image_builder_uses_stable_defaults_or_explicit_beta(
    monkeypatch: pytest.MonkeyPatch, beta: bool
) -> None:
    for name in (
        "HAOS_BUILD_OS_VERSION",
        "HAOS_BUILD_SUPERVISOR_CHANNEL",
        "HAOS_BUILD_SUPERVISOR_MIN_VERSION",
        "HAOS_BUILD_CORE_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)
    if beta:
        monkeypatch.setenv("HAOS_BUILD_OS_VERSION", "18.3.rc1")
        monkeypatch.setenv("HAOS_BUILD_SUPERVISOR_CHANNEL", "beta")
        monkeypatch.setenv("HAOS_BUILD_SUPERVISOR_MIN_VERSION", "2026.09.0")
        monkeypatch.setenv("HAOS_BUILD_CORE_VERSION", "2026.10.0b1")
    builder = runpy.run_path(str(_BUILDER))
    os_version = "18.3.rc1" if beta else builder["STABLE_HAOS_VERSION"]
    assert builder["HAOS_QCOW2_URL"] == (
        "https://github.com/home-assistant/operating-system/releases/download/"
        f"{os_version}/haos_ova-{os_version}.qcow2.xz"
    )
    assert builder["SUPERVISOR_CHANNEL"] == ("beta" if beta else "stable")
    assert builder["SUPERVISOR_MIN_VERSION"] == (
        "2026.09.0" if beta else builder["STABLE_SUPERVISOR_VERSION"]
    )
    assert builder["CORE_VERSION"] == (
        "2026.10.0b1" if beta else builder["STABLE_CORE_VERSION"]
    )


@pytest.mark.parametrize("version", ["", "../../other", "18.3; echo bad", "None"])
def test_image_builder_rejects_invalid_os_override(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setenv("HAOS_BUILD_OS_VERSION", version)
    with pytest.raises(ValueError, match="Invalid HAOS version"):
        runpy.run_path(str(_BUILDER))


def _run_beta_step(
    tmp_path: Path, job: str, step: str, beta: dict[str, Any]
) -> tuple[subprocess.CompletedProcess[str], str]:
    stable = {
        "supervisor": "2026.08.0",
        "homeassistant": {"qemux86-64": "2026.9.1"},
        "hassos": {"ova": "18.2"},
    }
    curl = tmp_path / "curl"
    curl.write_text(
        '#!/bin/bash\ncase "${@: -1}" in\n'
        '  https://version.home-assistant.io/beta.json) printf "%s" "$TEST_BETA" ;;\n'
        '  https://version.home-assistant.io/stable.json) printf "%s" "$TEST_STABLE" ;;\n'
        "  *) exit 99 ;;\nesac\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/haos-e2e-beta-tests.yml").read_text()
    )
    script = next(
        s["run"] for s in workflow["jobs"][job]["steps"] if s.get("id") == step
    )
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TEST_BETA": json.dumps(beta),
            "TEST_STABLE": json.dumps(stable),
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return result, output.read_text() if output.exists() else ""


@pytest.mark.parametrize(
    ("os_version", "supervisor", "core", "expected"),
    [
        ("18.2", "2026.08.0", "2026.9.1", "true"),
        ("18.3.rc1", "2026.08.0", "2026.9.1", "false"),
        ("18.2", "2026.09.0", "2026.9.1", "false"),
        ("18.2", "2026.08.0", "2026.10.0b1", "false"),
        (None, "2026.08.0", "2026.9.1", None),
        ("garbage", "2026.08.0", "2026.9.1", None),
    ],
)
def test_beta_gate_compares_all_three_releases(
    tmp_path: Path,
    os_version: str | None,
    supervisor: str,
    core: str,
    expected: str | None,
) -> None:
    result, output = _run_beta_step(
        tmp_path,
        "resolve-beta",
        "compare",
        {
            "supervisor": supervisor,
            "homeassistant": {"qemux86-64": core},
            "hassos": {"ova": os_version},
        },
    )
    if expected is None:
        assert result.returncode != 0
        assert "superfluous=true" not in output
    else:
        assert result.returncode == 0, result.stderr
        assert output == f"superfluous={expected}\n"


@pytest.mark.parametrize("job", ["haos-e2e-inaddon-beta", "haos-e2e-embedded-beta"])
def test_beta_resolver_emits_os_for_build_and_cache(tmp_path: Path, job: str) -> None:
    result, output = _run_beta_step(
        tmp_path,
        job,
        "versions",
        {
            "supervisor": "2026.09.0",
            "homeassistant": {"qemux86-64": "2026.10.0b1"},
            "hassos": {"ova": "18.3.rc1"},
        },
    )
    assert result.returncode == 0, result.stderr
    assert set(output.splitlines()) == {
        "os_version=18.3.rc1",
        "supervisor_version=2026.09.0",
        "core_version=2026.10.0b1",
    }
