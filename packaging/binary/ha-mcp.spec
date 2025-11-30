# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for ha-mcp standalone binary.

This creates a single-file executable that bundles the entire ha-mcp
MCP server with all dependencies. No Python installation required.

Build with: pyinstaller packaging/binary/ha-mcp.spec
"""

import os
import sys
import sysconfig
from PyInstaller.utils.hooks import collect_all

# Get project root (spec file is in packaging/binary/)
SPEC_DIR = os.path.dirname(os.path.abspath(SPECPATH))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SPEC_DIR))

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

# Add commonly missing modules for PyInstaller
hiddenimports += [
    # IDNA codec (required for httpx URL parsing)
    'idna.codec',
    'encodings.idna',
    # Additional encodings that may be needed
    'encodings.utf_8',
    'encodings.ascii',
    'encodings.latin_1',
    'encodings.punycode',
    # SSL/TLS support
    'ssl',
    '_ssl',
    # Async backends
    'asyncio',
    'asyncio.base_events',
    'asyncio.events',
    # JSON support
    'json',
    '_json',
    # Multiprocessing (sometimes needed)
    'multiprocessing.resource_tracker',
    'multiprocessing.sharedctypes',
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'src/ha_mcp/__main__.py')],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPEC_DIR, 'pyinstaller_hooks/runtime_hook.py')],  # Register codecs early
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
