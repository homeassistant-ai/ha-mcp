"""Guards for the vendored FastMCP / MCP SDK copies.

ha-mcp imports only ``ha_mcp._vendor.{fastmcp,mcp,mcp_types}`` so Home
Assistant Core's own ``mcp`` pin can never conflict with the server's.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parents[3]
_VENDOR = _REPO_ROOT / "src" / "ha_mcp" / "_vendor"
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import vendor_fastmcp  # noqa: E402

_VENDORED_DISTRIBUTIONS = {"fastmcp", "fastmcp-slim", "mcp", "mcp-types"}
# Declared by fastmcp-slim's server extra but never imported by the vendored
# code; ha-mcp must not pin the shared copy (#2135/#2146).
_NOT_DECLARED = {"websockets"}
# These drive the component's LLM-API client, which uses HA's shared SDK.
_SHARED_SDK_USERS = {
    "tests/src/unit/test_llm_api.py",
    "tests/src/e2e/workflows/embedded/test_embedded_server.py",
}


def _pins() -> dict[str, str]:
    return vendor_fastmcp.read_pins()


def _vendored_requirements(package: str) -> list[Requirement]:
    lines = (_VENDOR / package / vendor_fastmcp.REQUIRES_NAME).read_text("utf-8")
    return [Requirement(line) for line in lines.splitlines() if line.strip()]


def _declared() -> list[Requirement]:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    return [Requirement(d) for d in pyproject["project"]["dependencies"]]


@pytest.mark.parametrize("spec", vendor_fastmcp.PACKAGES, ids=lambda s: s.package)
class TestVendoredTreeMatchesPin:
    def test_marker_matches_the_pin(self, spec):
        marker = (_VENDOR / spec.package / "VENDORED").read_text(encoding="utf-8")
        assert marker.startswith(f"{spec.distribution}=={_pins()[spec.distribution]}\n")

    def test_license_ships_with_the_tree(self, spec):
        assert (_VENDOR / spec.package / "LICENSE").stat().st_size > 0

    def test_tree_matches_the_recorded_manifest(self, spec):
        tree = _VENDOR / spec.package
        recorded = (tree / vendor_fastmcp.MANIFEST_NAME).read_text(encoding="utf-8")
        assert vendor_fastmcp.manifest_lines(tree) == recorded.splitlines(), (
            f"vendored {spec.package} was edited by hand or partially synced — "
            "re-run scripts/vendor_fastmcp.py and commit the result"
        )


def test_versions_report_the_pins():
    from ha_mcp._vendor import fastmcp

    assert fastmcp.__version__ == _pins()["fastmcp-slim"]


def test_loggers_keep_upstream_names():
    from ha_mcp._vendor.fastmcp.server import server
    from ha_mcp._vendor.mcp.server import streamable_http

    assert server.logger.name == "fastmcp.server.server"
    assert streamable_http.logger.name == "mcp.server.streamable_http"
    assert isinstance(logging.getLogger("fastmcp.server.server"), logging.Logger)


def _first_party_files() -> list[Path]:
    roots = (
        _REPO_ROOT / "src" / "ha_mcp",
        _REPO_ROOT / "tests",
        _REPO_ROOT / "scripts",
    )
    return [
        path
        for root in roots
        for path in root.rglob("*.py")
        if _VENDOR not in path.parents
        and path.relative_to(_REPO_ROOT).as_posix() not in _SHARED_SDK_USERS
    ]


def test_no_first_party_module_imports_the_shared_packages():
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}: {names}"
        for path in _first_party_files()
        if (names := vendor_fastmcp.remaining_vendored_imports(path.read_text("utf-8")))
    ]
    assert not offenders, f"import ha_mcp._vendor.<pkg> instead: {offenders}"


_ISOLATION_PROBE = """
import json, os, sys, tempfile
os.environ.setdefault("HOMEASSISTANT_URL", "http://127.0.0.1:9")
os.environ.setdefault("HOMEASSISTANT_TOKEN", "unused")
os.environ["HA_MCP_CONFIG_DIR"] = tempfile.mkdtemp()
import ha_mcp.__main__
from ha_mcp.server import HomeAssistantSmartMCPServer
HomeAssistantSmartMCPServer().mcp.http_app(path="/mcp", stateless_http=True)
shared = sorted(
    name for name in sys.modules
    if name.split(".")[0] in {"fastmcp", "mcp", "mcp_types"}
)
print(json.dumps(shared))
"""


def test_building_the_server_loads_no_shared_fastmcp_or_mcp():
    """Runtime proof: nothing (dynamic imports included) reaches the shared copies.

    The dev environment installs a shared ``mcp`` 1.x, so a leak would import
    cleanly here — and bind to Home Assistant Core's copy in production.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    shared = json.loads(completed.stdout.strip().splitlines()[-1])
    assert shared == [], f"shared SDK modules loaded: {shared}"


def test_component_never_imports_the_vendored_copies():
    component = _REPO_ROOT / "custom_components" / "ha_mcp_tools"
    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path in component.rglob("*.py")
        if any(
            (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("ha_mcp._vendor")
            )
            or (
                isinstance(node, ast.Import)
                and any(a.name.startswith("ha_mcp._vendor") for a in node.names)
            )
            for node in ast.walk(ast.parse(path.read_text("utf-8")))
        )
    ]
    assert not offenders, f"the HA main process must not import ha_mcp: {offenders}"


def test_pyproject_declares_no_shared_fastmcp_or_mcp():
    declared = {canonicalize_name(r.name) for r in _declared()}
    assert not declared & _VENDORED_DISTRIBUTIONS


def _floor(requirement: Requirement) -> Version | None:
    versions = [
        Version(s.version)
        for s in requirement.specifier
        if s.operator in {">=", ">", "==", "~="}
    ]
    return max(versions, default=None)


def _ceiling(requirement: Requirement) -> Version | None:
    versions = [
        Version(s.version)
        for s in requirement.specifier
        if s.operator in {"<", "<=", "=="}
    ]
    return min(versions, default=None)


@pytest.mark.parametrize("spec", vendor_fastmcp.PACKAGES, ids=lambda s: s.package)
def test_pyproject_covers_each_vendored_runtime_requirement(spec):
    """Every requirement the vendored wheels declare is a direct dependency.

    Checked against the requirement list the sync records from each wheel, so
    a bump that adds a dependency or raises a floor fails here rather than
    only inside Home Assistant, where Core's constraints can hold an older
    version back.
    """
    declared: dict[str, list[Requirement]] = {}
    for requirement in _declared():
        declared.setdefault(canonicalize_name(requirement.name), []).append(requirement)
    problems = []
    for needed in _vendored_requirements(spec.package):
        name = canonicalize_name(needed.name)
        if name in _VENDORED_DISTRIBUTIONS or name in _NOT_DECLARED:
            continue
        candidates = declared.get(name, [])
        if not candidates:
            problems.append(f"{needed} is not declared")
            continue
        floor, ceiling = _floor(needed), _ceiling(needed)
        ok = any(
            (floor is None or ((own := _floor(c)) is not None and own >= floor))
            and (
                ceiling is None or ((top := _ceiling(c)) is not None and top <= ceiling)
            )
            for c in candidates
        )
        if not ok:
            problems.append(
                f"{needed} is not covered by {[str(c) for c in candidates]}"
            )
    assert not problems, problems


def test_vendored_packages_agree_on_each_others_pins():
    pins = _pins()
    mcp_needs = {canonicalize_name(r.name): r for r in _vendored_requirements("mcp")}
    assert mcp_needs["mcp-types"].specifier.contains(pins["mcp-types"])
    fastmcp_needs = {
        canonicalize_name(r.name): r for r in _vendored_requirements("fastmcp")
    }
    assert fastmcp_needs["mcp"].specifier.contains(pins["mcp"])
    assert fastmcp_needs["mcp-types"].specifier.contains(pins["mcp-types"])
