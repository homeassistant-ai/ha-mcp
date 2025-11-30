# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for ha-mcp standalone binary.

This creates a single-file executable that bundles the entire ha-mcp
MCP server with all dependencies. No Python installation required.

Build with: pyinstaller ha-mcp.spec
"""

import os
import sys
import sysconfig
from PyInstaller.utils.hooks import collect_all

# Find Python stdlib path dynamically
stdlib_path = sysconfig.get_paths()['stdlib']

# Stdlib modules that need to be included as data files
# (PyInstaller doesn't always bundle these correctly)
stdlib_modules = ['pickletools.py', 'webbrowser.py', 'difflib.py']
stdlib_dirs = ['sqlite3']

datas = []
for module in stdlib_modules:
    module_path = os.path.join(stdlib_path, module)
    if os.path.exists(module_path):
        datas.append((module_path, '.'))

for dir_name in stdlib_dirs:
    dir_path = os.path.join(stdlib_path, dir_name)
    if os.path.exists(dir_path):
        datas.append((dir_path, dir_name))

binaries = []
hiddenimports = []

# Collect all dependencies
packages_to_collect = [
    'ha_mcp',
    'fastmcp',
    'httpx',
    'httpcore',
    'h11',
    'pydantic',
    'pydantic_core',
    'diskcache',
    'key_value',
    'beartype',
    'pathvalidate',
    'exceptiongroup',
    'cachetools',
    'anyio',
    'sniffio',
    'certifi',
    'idna',
    'websockets',
    'sse_starlette',
    'starlette',
    'uvicorn',
    'textdistance',
    'annotated_types',
    'typing_extensions',
]

for package in packages_to_collect:
    try:
        tmp_ret = collect_all(package)
        datas += tmp_ret[0]
        binaries += tmp_ret[1]
        hiddenimports += tmp_ret[2]
    except Exception as e:
        print(f"Warning: Could not collect {package}: {e}")

# Add specific hidden imports for mcp (avoid mcp.cli which requires typer)
hiddenimports += [
    'mcp',
    'mcp.client',
    'mcp.server',
    'mcp.types',
    'mcp.shared',
]

# Add idna codec modules (required for httpx URL parsing)
hiddenimports += [
    'idna.codec',
    'encodings.idna',
]

a = Analysis(
    ['src/ha_mcp/__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['mcp.cli', 'typer'],  # Keep click - uvicorn needs it
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ha-mcp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
