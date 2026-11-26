"""A Core bump moves the dev group's integration pins without a hand edit.

Unit tests run Core's own handlers for the integrations the script lists, so
the dev group installs their requirements at Core's pins. A Core update that
moves a manifest pin must move the dev pin with it, or the tests run a library
that Core version never ships with.
"""

from __future__ import annotations

import ast
import io
import json
import shutil
import sys
import tomllib
import urllib.error
from collections import Counter
from collections.abc import Mapping
from email.message import Message
from importlib.metadata import version as installed_version
from pathlib import Path

import homeassistant.components
import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import sync_dev_integration_pins as sync

_REPO = Path(__file__).resolve().parents[3]
_COMPONENTS = Path(homeassistant.components.__file__).parent
_MANIFEST_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/"
    "{version}/homeassistant/components/{domain}/manifest.json"
)
_PYPROJECT = """\
[dependency-groups]
dev = [
    "homeassistant==2026.12.0",
    # core-integration-pins:start
    "zha==2.4.3",
    "dropped-by-core==1.0.0",
    # core-integration-pins:end
    "ruff==0.14.0",
]
"""
_DEPENDABOT = """\
updates:
  - package-ecosystem: "uv"
    ignore:
      - dependency-name: "mcp"
      # core-integration-pins:start
      - dependency-name: "zha"
      - dependency-name: "dropped-by-core"
      # core-integration-pins:end
"""


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    pyproject = tmp_path / "pyproject.toml"
    dependabot = tmp_path / ".github" / "dependabot.yml"
    dependabot.parent.mkdir()
    pyproject.write_text(_PYPROJECT, encoding="utf-8")
    dependabot.write_text(_DEPENDABOT, encoding="utf-8")
    return pyproject, dependabot


def _installed_manifests() -> dict[str, str]:
    return {
        domain: (_COMPONENTS / domain / "manifest.json").read_text(encoding="utf-8")
        for domain in sync.INTEGRATIONS
    }


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    manifests: Mapping[str, str | Exception],
) -> list[str]:
    """Answer the script's GitHub requests for that Core version; return the URLs asked."""
    answers = {
        _MANIFEST_URL.format(version=version, domain=domain): answer
        for domain, answer in manifests.items()
    }
    requested: list[str] = []

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        requested.append(url)
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer.encode())

    monkeypatch.setattr(sync.checker.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sync.checker.time, "sleep", lambda _seconds: None)
    return requested


def _manifests(**requirements: list[str]) -> dict[str, str | Exception]:
    return {
        domain: json.dumps({"requirements": requirements.get(domain, [])})
        for domain in sync.INTEGRATIONS
    }


def test_committed_pins_are_what_the_sync_writes_for_the_dev_group_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Otherwise the tests run a library the dev group's Core never ships with,
    # and Renovate's next Core bump also carries the correction.
    committed = _REPO / "pyproject.toml", _REPO / ".github" / "dependabot.yml"
    synced = tmp_path / "pyproject.toml", tmp_path / ".github" / "dependabot.yml"
    synced[1].parent.mkdir()
    for source, target in zip(committed, synced, strict=True):
        shutil.copyfile(source, target)
    version = sync.checker.dev_group_core_version(committed[0].read_text("utf-8"))
    if version != installed_version("homeassistant"):
        pytest.skip("The committed pins follow the stable dev group")
    _serve(monkeypatch, version, _installed_manifests())

    assert sync.main(tmp_path) == 0

    for source, target in zip(committed, synced, strict=True):
        assert target.read_text("utf-8") == source.read_text("utf-8")


def test_installed_integration_dependencies_match_running_core() -> None:
    for text in sync.manifest_requirements(_installed_manifests().values()).values():
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate():
            assert installed_version(requirement.name) in requirement.specifier, text


def test_marked_blocks_sit_in_the_dev_group_and_the_uv_ignore_list() -> None:
    # A block anywhere else is rewritten faithfully and does nothing.
    names = set(sync.manifest_requirements(_installed_manifests().values()))
    project = tomllib.loads((_REPO / "pyproject.toml").read_text("utf-8"))
    dev = Counter(
        canonicalize_name(Requirement(entry).name)
        for entry in project["dependency-groups"]["dev"]
        if isinstance(entry, str)
    )
    dependabot = yaml.safe_load(
        (_REPO / ".github" / "dependabot.yml").read_text("utf-8")
    )
    uv = next(u for u in dependabot["updates"] if u["package-ecosystem"] == "uv")
    ignored = {e["dependency-name"] for e in uv["ignore"] if "update-types" not in e}

    assert {name: dev[name] for name in names} == dict.fromkeys(names, 1)
    assert names - ignored == set()


def test_core_bump_moves_adds_and_drops_pins_in_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject, dependabot = _repo(tmp_path)
    requested = _serve(
        monkeypatch,
        "2026.12.0",
        _manifests(
            zha=["zha==2.5.0", "New_Lib[b]==0.1.0"],
            knx=["new-lib[a]==0.1.0", 'marked==1.0; python_version < "3.15"'],
        ),
    )

    assert sync.main(tmp_path) == 0

    dev = tomllib.loads(pyproject.read_text("utf-8"))["dependency-groups"]["dev"]
    assert dev == [
        "homeassistant==2026.12.0",
        'marked==1.0; python_version < "3.15"',
        "New_Lib[a,b]==0.1.0",
        "zha==2.5.0",
        "ruff==0.14.0",
    ]
    ignore = yaml.safe_load(dependabot.read_text("utf-8"))["updates"][0]["ignore"]
    assert [entry["dependency-name"] for entry in ignore] == [
        "mcp",
        "marked",
        "new-lib",
        "zha",
    ]
    assert set(requested) == {
        _MANIFEST_URL.format(version="2026.12.0", domain=domain)
        for domain in sync.INTEGRATIONS
    }


def test_one_library_pinned_two_ways_is_refused() -> None:
    manifests = [
        json.dumps({"requirements": [pin]}) for pin in ("zha==2.5.0", "zha==2.4.3")
    ]

    with pytest.raises(SystemExit, match="zha"):
        sync.manifest_requirements(manifests)


def test_unreachable_manifest_fails_and_leaves_the_pins_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject, dependabot = _repo(tmp_path)
    manifests = _manifests(otbr=["python-otbr-api==9.9.9"])
    manifests["zha"] = urllib.error.URLError("network down")
    _serve(monkeypatch, "2026.12.0", manifests)

    assert sync.main(tmp_path) == 1

    assert pyproject.read_text("utf-8") == _PYPROJECT
    assert dependabot.read_text("utf-8") == _DEPENDABOT


def test_integration_core_dropped_is_named_and_leaves_the_pins_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pyproject, dependabot = _repo(tmp_path)
    manifests = _manifests(otbr=["python-otbr-api==9.9.9"])
    manifests["zha"] = urllib.error.HTTPError("", 404, "Not Found", Message(), None)
    _serve(monkeypatch, "2026.12.0", manifests)

    with pytest.raises(SystemExit):
        sync.main(tmp_path)

    assert "'zha' manifest; update INTEGRATIONS" in capsys.readouterr().err
    assert pyproject.read_text("utf-8") == _PYPROJECT
    assert dependabot.read_text("utf-8") == _DEPENDABOT


def test_lost_marker_in_one_file_leaves_the_other_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject, dependabot = _repo(tmp_path)
    dependabot.write_text(_DEPENDABOT.replace("pins:end", "pins"), encoding="utf-8")
    _serve(monkeypatch, "2026.12.0", _manifests(zha=["zha==2.5.0"]))

    with pytest.raises(SystemExit, match="core-integration-pins:end"):
        sync.main(tmp_path)

    assert pyproject.read_text("utf-8") == _PYPROJECT


@pytest.mark.parametrize(
    "text",
    [
        "# core-integration-pins:end\n",
        "# core-integration-pins:start\n",
        "# core-integration-pins:end\n# core-integration-pins:start\n",
    ],
    ids=["no start", "no end", "end first"],
)
def test_misplaced_markers_are_refused(text: str) -> None:
    with pytest.raises(SystemExit, match="core-integration-pins"):
        sync.replace_block(text, ['"zha==2.5.0",'])


@pytest.mark.parametrize(
    "script", ["sync_dev_integration_pins.py", "check_ha_constraint_alignment.py"]
)
def test_sync_imports_only_what_renovate_installs(script: str) -> None:
    # Renovate runs it with `uv run --no-project --with packaging`: any other
    # import fails there and nowhere else.
    tree = ast.parse((_REPO / "scripts" / script).read_text("utf-8"))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    allowed = sys.stdlib_module_names | {"packaging", "check_ha_constraint_alignment"}

    assert {name.split(".")[0] for name in imported} - allowed == set()
