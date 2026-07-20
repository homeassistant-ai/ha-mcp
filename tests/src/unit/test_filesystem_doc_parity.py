"""Doc <-> allowlist parity guard for the filesystem tools (issue #1965).

Every directory in the component's ``ALLOWED_READ_DIRS`` / ``ALLOWED_WRITE_DIRS``
must be documented in both the server tool docstrings
(``ha_read_file`` / ``ha_list_files`` / ``ha_write_file`` / ``ha_delete_file`` in
``src/ha_mcp/tools/tools_filesystem.py``) and the component's ``services.yaml``
``path`` field descriptions. This catches the drift where a directory is added to
the allowlist but a doc surface is forgotten -- exactly what happened for
``dashboards/`` and is being corrected here for ``blueprints/``.

Self-contained on purpose: it parses the sources with ``ast`` / ``yaml`` and
imports nothing from Home Assistant or fastmcp, so it runs in CI *and* locally
without either installed.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPONENT_DIR = _REPO_ROOT / "custom_components" / "ha_mcp_tools"
_TOOLS_FILE = _REPO_ROOT / "src" / "ha_mcp" / "tools" / "tools_filesystem.py"
_SERVICES_FILE = _COMPONENT_DIR / "services.yaml"


def _load_const_standalone():
    """Load ``const.py`` directly, bypassing the HA-dependent package __init__."""
    spec = importlib.util.spec_from_file_location(
        "_ha_mcp_tools_const_parity", _COMPONENT_DIR / "const.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CONST = _load_const_standalone()
ALLOWED_READ_DIRS = _CONST.ALLOWED_READ_DIRS
ALLOWED_WRITE_DIRS = _CONST.ALLOWED_WRITE_DIRS


def _tool_docstrings():
    """Map every top-level/def name in tools_filesystem.py to its docstring."""
    tree = ast.parse(_TOOLS_FILE.read_text(encoding="utf-8"))
    docs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            if doc is not None:
                docs[node.name] = doc
    return docs


def _service_path_descriptions():
    """Map each service name to its ``path`` field description in services.yaml."""
    data = yaml.safe_load(_SERVICES_FILE.read_text(encoding="utf-8"))
    return {
        name: (svc.get("fields", {}).get("path", {}).get("description", "") or "")
        for name, svc in data.items()
    }


_DOCS = _tool_docstrings()
_SERVICE_PATH_DESCS = _service_path_descriptions()

_READ_TOOLS = ("ha_read_file", "ha_list_files")
_WRITE_TOOLS = ("ha_write_file", "ha_delete_file")
_READ_SERVICES = ("read_file", "list_files")
_WRITE_SERVICES = ("write_file", "delete_file")


@pytest.mark.parametrize("tool", _READ_TOOLS)
@pytest.mark.parametrize("directory", ALLOWED_READ_DIRS)
def test_read_dirs_documented_in_tool_docstrings(directory, tool):
    assert f"{directory}/" in _DOCS[tool], (
        f"{directory!r} is in ALLOWED_READ_DIRS but not documented in the "
        f"{tool} docstring (tools_filesystem.py)"
    )


@pytest.mark.parametrize("tool", _WRITE_TOOLS)
@pytest.mark.parametrize("directory", ALLOWED_WRITE_DIRS)
def test_write_dirs_documented_in_tool_docstrings(directory, tool):
    assert f"{directory}/" in _DOCS[tool], (
        f"{directory!r} is in ALLOWED_WRITE_DIRS but not documented in the "
        f"{tool} docstring (tools_filesystem.py)"
    )


@pytest.mark.parametrize("service", _READ_SERVICES)
@pytest.mark.parametrize("directory", ALLOWED_READ_DIRS)
def test_read_dirs_documented_in_services_yaml(directory, service):
    assert f"{directory}/" in _SERVICE_PATH_DESCS[service], (
        f"{directory!r} is in ALLOWED_READ_DIRS but not documented in the "
        f"services.yaml {service} path description"
    )


@pytest.mark.parametrize("service", _WRITE_SERVICES)
@pytest.mark.parametrize("directory", ALLOWED_WRITE_DIRS)
def test_write_dirs_documented_in_services_yaml(directory, service):
    assert f"{directory}/" in _SERVICE_PATH_DESCS[service], (
        f"{directory!r} is in ALLOWED_WRITE_DIRS but not documented in the "
        f"services.yaml {service} path description"
    )
