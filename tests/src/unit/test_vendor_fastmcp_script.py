"""Unit tests for scripts/vendor_fastmcp.py's import rewriting and patching."""

from __future__ import annotations

import ast
import io
import sys
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import vendor_fastmcp as vendor  # noqa: E402


def _bound_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update((a.asname or a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update((a.asname or a.name) for a in node.names)
    return names


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from fastmcp import FastMCP\n",
            "from ha_mcp._vendor.fastmcp import FastMCP\n",
        ),
        (
            "from mcp.types import Tool as T\n",
            "from ha_mcp._vendor.mcp.types import Tool as T\n",
        ),
        ("import mcp_types\n", "from ha_mcp._vendor import mcp_types\n"),
        (
            "import mcp.types\n",
            "import ha_mcp._vendor.mcp.types; from ha_mcp._vendor import mcp\n",
        ),
        ("import fastmcp as fm\n", "import ha_mcp._vendor.fastmcp as fm\n"),
        ("import os, mcp\n", "import os; from ha_mcp._vendor import mcp\n"),
        ("from . import mcp\n", "from . import mcp\n"),
        ("from mcpx import y\n", "from mcpx import y\n"),
    ],
)
def test_rewrite_imports(source, expected):
    assert vendor.rewrite_imports(source) == expected


def test_rewrite_preserves_bindings_and_indentation():
    source = (
        "def f():\n"
        "    import mcp.types\n"
        "    from fastmcp import (\n"
        "        Client,  # comment\n"
        "    )\n"
        "    return mcp.types, Client\n"
    )
    rewritten = vendor.rewrite_imports(source)
    assert _bound_names(rewritten) == _bound_names(source) | {"ha_mcp"}
    assert (
        "    from ha_mcp._vendor.fastmcp import (\n        Client,  # comment\n"
        in rewritten
    )
    assert vendor.remaining_vendored_imports(rewritten) == []


def test_rewrite_leaves_strings_and_comments_alone():
    source = '# from fastmcp import x\ns = "import mcp"\n"""from mcp import y"""\n'
    assert vendor.rewrite_imports(source) == source


def test_rewrite_handles_non_ascii_before_the_import():
    source = 'x = "é"; import mcp\n'
    assert vendor.rewrite_imports(source) == 'x = "é"; from ha_mcp._vendor import mcp\n'


def test_rewrite_dynamic_imports():
    source = (
        'importlib.import_module("fastmcp.server")\n'
        'importlib.import_module(f"fastmcp.server.{name}")\n'
        'importlib.import_module("fastmcpx")\n'
    )
    assert vendor.rewrite_imports(source) == (
        'importlib.import_module("ha_mcp._vendor.fastmcp.server")\n'
        'importlib.import_module(f"ha_mcp._vendor.fastmcp.server.{name}")\n'
        'importlib.import_module("fastmcpx")\n'
    )


_PINS = {"fastmcp-slim": "9.9.9", "mcp": "8.8.8", "mcp-types": "8.8.8"}
_FASTMCP_INIT = (
    "from importlib.metadata import PackageNotFoundError, version as _version\n"
    'try:\n    __version__ = _version("fastmcp-slim")\n'
    'except PackageNotFoundError:\n    __version__ = _version("fastmcp")\n'
)


def test_patch_pins_the_fastmcp_version():
    patched = vendor.patch_package("fastmcp", "__init__.py", _FASTMCP_INIT, _PINS)
    assert '__version__ = "9.9.9"\n' in patched
    assert "_version(" not in patched


def test_patch_fails_loudly_when_the_version_lookup_changes_shape():
    with pytest.raises(SystemExit, match="changed shape"):
        vendor.patch_package("fastmcp", "__init__.py", "__version__ = x\n", _PINS)


def test_patch_keeps_upstream_logger_names():
    source = "logger = get_logger(__name__)\nlog = logging.getLogger(name=__name__)\n"
    patched = vendor.patch_package("mcp", "server/x.py", source, _PINS)
    assert patched.count('__name__.removeprefix("ha_mcp._vendor.")') == 2


def _wheel(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        for name, body in members.items():
            wheel.writestr(name, body)
    return buffer.getvalue()


def test_stage_package_rewrites_and_records_a_manifest(tmp_path):
    spec = vendor.VendoredPackage("mcp", "mcp")
    wheel = _wheel(
        {
            "mcp/__init__.py": "from mcp.types import Tool\n",
            "mcp/types.py": "Tool = object\n",
            "mcp/__pycache__/types.cpython-313.pyc": "x",
            "mcp-8.8.8.dist-info/METADATA": "",
        }
    )
    staging = tmp_path / "mcp"
    count = vendor.stage_package(spec, wheel, b"MIT", _PINS, staging)
    assert count == 2
    assert (
        staging / "__init__.py"
    ).read_text() == "from ha_mcp._vendor.mcp.types import Tool\n"
    assert (staging / "VENDORED").read_text().startswith("mcp==8.8.8\n")
    assert (staging / "LICENSE").read_bytes() == b"MIT"
    recorded = (staging / vendor.MANIFEST_NAME).read_text().splitlines()
    assert recorded == vendor.manifest_lines(staging)
    assert not (staging / "__pycache__").exists()


def test_stage_package_refuses_path_traversal(tmp_path):
    spec = vendor.VendoredPackage("mcp", "mcp")
    wheel = _wheel({"mcp/__init__.py": "", "mcp/../../evil.py": ""})
    with pytest.raises(SystemExit, match="escaping"):
        vendor.stage_package(spec, wheel, b"MIT", _PINS, tmp_path / "mcp")


def test_promote_restores_every_tree_when_a_swap_fails(tmp_path, monkeypatch):
    live_a, live_b = tmp_path / "a", tmp_path / "b"
    for live in (live_a, live_b):
        live.mkdir()
        (live / "old").write_text("old")
        staged = live.with_name(live.name + ".incoming")
        staged.mkdir()
        (staged / "new").write_text("new")
    real_rename = Path.rename

    def failing_rename(self, target):
        if self == tmp_path / "b.incoming":
            raise OSError("simulated")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError):
        vendor._promote(
            {live_a: tmp_path / "a.incoming", live_b: tmp_path / "b.incoming"}
        )
    monkeypatch.undo()
    assert (live_a / "old").is_file() and not (live_a / "new").exists()
    assert (live_b / "old").is_file()


def test_rewrite_handles_a_backslash_continued_from_import():
    source = "from \\\n    mcp.types import Tool\n"
    assert vendor.rewrite_imports(source) == (
        "from \\\n    ha_mcp._vendor.mcp.types import Tool\n"
    )


def test_patch_replaces_every_version_lookup_of_a_vendored_distribution():
    source = 'v = importlib.metadata.version("mcp")\nw = importlib.metadata.version("fastmcp")\n'
    patched = vendor.patch_package("mcp", "cli/cli.py", source, _PINS)
    assert patched == 'v = "8.8.8"\nw = "9.9.9"\n'


_TRACEBACK_SOURCE = 'for package in ("fastmcp", "mcp", "pydantic")\n'


def test_patch_points_traceback_suppression_at_the_vendored_packages():
    patched = vendor.patch_package(
        "fastmcp", "utilities/logging.py", _TRACEBACK_SOURCE, _PINS
    )
    assert '"ha_mcp._vendor.fastmcp", "ha_mcp._vendor.mcp", "pydantic"' in patched


def test_patch_fails_loudly_when_traceback_suppression_changes_shape():
    with pytest.raises(SystemExit, match="changed shape"):
        vendor.patch_package("fastmcp", "utilities/logging.py", "x = 1\n", _PINS)


def test_stage_package_refuses_an_unpatched_package_lookup(tmp_path):
    spec = vendor.VendoredPackage("mcp", "mcp")
    wheel = _wheel(
        {
            "mcp/__init__.py": 'import importlib.util\nimportlib.util.find_spec("mcp")\n',
            "mcp-8.8.8.dist-info/METADATA": "",
        }
    )
    with pytest.raises(SystemExit, match="unpatched package lookup"):
        vendor.stage_package(spec, wheel, b"MIT", _PINS, tmp_path / "mcp")


def test_runtime_requirements_keep_base_and_selected_extras():
    metadata = "\n".join(
        [
            "Metadata-Version: 2.4",
            "Requires-Dist: pydantic>=2.12",
            "Requires-Dist: anyio>=4.10; python_version >= '3.14'",
            "Requires-Dist: httpx2>=2.5; extra == 'client'",
            "Requires-Dist: uvicorn>=0.35; sys_platform != 'emscripten' and extra == 'server'",
            "Requires-Dist: pywin32>=311; extra == 'server' and sys_platform == 'win32'",
            "Requires-Dist: openai>=1; extra == 'openai'",
        ]
    )
    wheel = _wheel({"pkg-1.0.dist-info/METADATA": metadata})
    assert vendor.runtime_requirements(wheel, ("client", "server")) == [
        "anyio>=4.10; python_version >= '3.14'",
        "httpx2>=2.5",
        "pydantic>=2.12",
        "pywin32>=311; sys_platform == 'win32'",
        "uvicorn>=0.35; sys_platform != 'emscripten'",
    ]


class _Release:
    def __init__(self, entries):
        self.entries = entries

    def download(self, url):
        if url.startswith("https://pypi.org/pypi/"):
            return __import__("json").dumps({"urls": self.entries}).encode()
        return b"wheel-bytes"


def test_download_wheel_refuses_a_digest_mismatch(monkeypatch):
    release = _Release(
        [
            {
                "packagetype": "bdist_wheel",
                "filename": "mcp-8.8.8-py3-none-any.whl",
                "url": "https://files.example/mcp.whl",
                "digests": {"sha256": "0" * 64},
            }
        ]
    )
    monkeypatch.setattr(vendor, "_download", release.download)
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        vendor._download_wheel("mcp", "8.8.8")


def test_download_wheel_refuses_a_release_without_a_pure_python_wheel(monkeypatch):
    release = _Release(
        [
            {
                "packagetype": "sdist",
                "filename": "mcp-8.8.8.tar.gz",
                "url": "https://files.example/x",
            }
        ]
    )
    monkeypatch.setattr(vendor, "_download", release.download)
    with pytest.raises(SystemExit, match="no pure-Python wheel"):
        vendor._download_wheel("mcp", "8.8.8")


def test_main_leaves_live_trees_untouched_when_a_download_fails(tmp_path, monkeypatch):
    vendor_dir = tmp_path / "_vendor"
    vendor_dir.mkdir()
    (vendor_dir / "requirements.txt").write_text(
        "fastmcp-slim==9.9.9\nmcp==8.8.8\nmcp-types==8.8.8\n", encoding="utf-8"
    )
    for spec in vendor.PACKAGES:
        (vendor_dir / spec.package).mkdir()
        (vendor_dir / spec.package / "live.py").write_text("", encoding="utf-8")

    good = _wheel(
        {
            "fastmcp/__init__.py": _FASTMCP_INIT,
            "fastmcp-9.9.9.dist-info/METADATA": "",
            "fastmcp-9.9.9.dist-info/licenses/LICENSE": "Apache",
        }
    )

    def download_wheel(distribution, version):
        if distribution == "mcp":
            raise SystemExit("network down")
        return good

    monkeypatch.setattr(vendor, "_download_wheel", download_wheel)
    with pytest.raises(SystemExit, match="network down"):
        vendor.main(vendor_dir)
    assert not list(vendor_dir.glob("*.incoming"))
    for spec in vendor.PACKAGES:
        assert (vendor_dir / spec.package / "live.py").is_file()
