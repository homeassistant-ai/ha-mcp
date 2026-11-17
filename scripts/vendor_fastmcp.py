"""Sync the vendored FastMCP / MCP SDK copies from their pinned versions.

ha-mcp imports private copies of ``fastmcp`` (the ``fastmcp-slim`` wheel),
``mcp`` and ``mcp_types`` under ``ha_mcp._vendor``, so Home Assistant Core's
own ``mcp`` pin can never conflict with ours. These packages import themselves
by absolute name, so every such import is rewritten to the vendored path.

Pins live in ``src/ha_mcp/_vendor/requirements.txt``; Renovate runs this script
on a bump and ``tests/src/unit/test_vendored_fastmcp.py`` rejects drift. The
sync also raises the ``pyproject.toml`` floors the new versions need; Renovate
re-locks afterwards.

Usage:
    python scripts/vendor_fastmcp.py
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VENDOR_DIR = _REPO_ROOT / "src" / "ha_mcp" / "_vendor"
_PIN_FILE = _VENDOR_DIR / "requirements.txt"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_PYPI_JSON_URL = "https://pypi.org/pypi/{name}/{version}/json"

VENDOR_PREFIX = "ha_mcp._vendor"
MANIFEST_NAME = "MANIFEST.sha256"
REQUIRES_NAME = "REQUIRES.txt"


@dataclass(frozen=True)
class VendoredPackage:
    distribution: str
    package: str
    # fastmcp-slim ships no LICENSE; the ``fastmcp`` wheel released at the same
    # version does.
    license_distribution: str | None = None
    # Extras whose requirements ha-mcp must provide.
    extras: tuple[str, ...] = ()


PACKAGES = (
    VendoredPackage(
        "fastmcp-slim",
        "fastmcp",
        license_distribution="fastmcp",
        extras=("client", "server"),
    ),
    VendoredPackage("mcp", "mcp"),
    VendoredPackage("mcp-types", "mcp_types"),
)
VENDORED_TOP_LEVEL = frozenset(p.package for p in PACKAGES)
_DISTRIBUTION_PIN = {"fastmcp": "fastmcp-slim", "fastmcp-slim": "fastmcp-slim"}

_TOP_ALTERNATION = "|".join(sorted(VENDORED_TOP_LEVEL))
_DYNAMIC_IMPORT_RE = re.compile(
    rf"""(import_module\(\s*f?)(["'])({_TOP_ALTERNATION})(?=[."'])"""
)
# Keeps logger names at their upstream spelling (fastmcp.*, mcp.*).
_LOGGER_NAME_RE = re.compile(
    r"\b(getLogger|get_logger)\(\s*(name\s*=\s*)?__name__\s*\)"
)
_METADATA_VERSION_RE = re.compile(
    r"""importlib\.metadata\.version\(\s*["'](fastmcp|fastmcp-slim|mcp|mcp-types)["']\s*\)"""
)
# Lookups by package name that the rewrite cannot redirect; any left after
# patching fail the sync.
_UNPATCHED_LOOKUP_RE = re.compile(
    r"""\b(?:version|distribution|metadata|find_spec)\(\s*["']"""
    r"""(fastmcp|fastmcp-slim|mcp|mcp-types|mcp_types)["']"""
)
_TRACEBACK_SUPPRESS = 'for package in ("fastmcp", "mcp", "pydantic")'


def iter_vendored_files(tree: Path) -> list[Path]:
    """Every vendored file except the manifest and bytecode, in stable order."""
    return sorted(
        (
            p
            for p in tree.rglob("*")
            if p.is_file() and p.name != MANIFEST_NAME and "__pycache__" not in p.parts
        ),
        key=lambda p: p.relative_to(tree).as_posix(),
    )


def manifest_lines(tree: Path) -> list[str]:
    return [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
        f"{path.relative_to(tree).as_posix()}"
        for path in iter_vendored_files(tree)
    ]


def _write_manifest(tree: Path) -> None:
    # LF on every platform so the manifest matches git's checkout on Linux CI.
    (tree / MANIFEST_NAME).write_text(
        "\n".join(manifest_lines(tree)) + "\n", encoding="utf-8", newline="\n"
    )


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(pin_file: Path = _PIN_FILE) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw_line in pin_file.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(
            r"([A-Za-z0-9._-]+)==([A-Za-z0-9.!+-]+)", raw_line.strip()
        ):
            pins[canonical(match.group(1))] = match.group(2)
    missing = [p.distribution for p in PACKAGES if p.distribution not in pins]
    if missing:
        raise SystemExit(f"no '<name>==<version>' pin for {missing} in {pin_file}")
    return pins


def _vendored(module: str) -> str:
    return f"{VENDOR_PREFIX}.{module}"


def _is_vendored(module: str | None) -> bool:
    return module is not None and module.split(".")[0] in VENDORED_TOP_LEVEL


def _rewrite_import(node: ast.Import) -> str | None:
    if not any(_is_vendored(alias.name) for alias in node.names):
        return None
    statements: list[str] = []
    for alias in node.names:
        if not _is_vendored(alias.name):
            as_clause = f" as {alias.asname}" if alias.asname else ""
            statements.append(f"import {alias.name}{as_clause}")
        elif alias.asname:
            statements.append(f"import {_vendored(alias.name)} as {alias.asname}")
        else:
            # ``import mcp.types`` binds ``mcp``; the vendored form must too.
            top = alias.name.split(".")[0]
            if alias.name != top:
                statements.append(f"import {_vendored(alias.name)}")
            statements.append(f"from {VENDOR_PREFIX} import {top}")
    return "; ".join(statements)


def _rewrite_import_from(node: ast.ImportFrom, text: str) -> str | None:
    if node.level or not _is_vendored(node.module):
        return None
    module = node.module or ""
    rewritten, count = re.subn(
        rf"\Afrom((?:\s|\\\n)+){re.escape(module)}\b",
        lambda m: f"from{m.group(1)}{_vendored(module)}",
        text,
    )
    if count != 1:
        raise ValueError(f"could not rewrite import statement: {text!r}")
    return rewritten


def _offset(source: str, line_starts: list[int], lineno: int, byte_col: int) -> int:
    """Character offset of an AST (line, UTF-8 byte column) position."""
    start = line_starts[lineno - 1]
    line = source[start : line_starts[lineno]]
    return start + len(line.encode("utf-8")[:byte_col].decode("utf-8"))


def rewrite_imports(source: str) -> str:
    """Point absolute imports of the vendored packages at ``ha_mcp._vendor``."""
    line_starts = [0]
    for line in source.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        assert node.end_lineno is not None and node.end_col_offset is not None
        start = _offset(source, line_starts, node.lineno, node.col_offset)
        end = _offset(source, line_starts, node.end_lineno, node.end_col_offset)
        text = source[start:end]
        replacement = (
            _rewrite_import(node)
            if isinstance(node, ast.Import)
            else _rewrite_import_from(node, text)
        )
        if replacement is not None and replacement != text:
            edits.append((start, end, replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return _DYNAMIC_IMPORT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_vendored(m.group(3))}", source
    )


def remaining_vendored_imports(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if _is_vendored(a.name))
        elif (
            isinstance(node, ast.ImportFrom)
            and not node.level
            and _is_vendored(node.module)
        ):
            found.append(node.module or "")
    return found


def patch_package(
    package: str, relative: str, source: str, pins: dict[str, str]
) -> str:
    """Replace lookups a vendored copy can't satisfy and keep upstream logger names.

    A vendored copy has no distribution metadata, so version lookups become the
    pinned versions, and name-based package lookups point at the vendored path.
    """
    if package == "fastmcp" and relative == "__init__.py":
        old = (
            'try:\n    __version__ = _version("fastmcp-slim")\n'
            'except PackageNotFoundError:\n    __version__ = _version("fastmcp")\n'
        )
        if source.count(old) != 1:
            raise SystemExit("fastmcp/__init__.py version lookup changed shape")
        source = source.replace(old, f'__version__ = "{pins["fastmcp-slim"]}"\n')
    if package == "fastmcp" and relative == "utilities/logging.py":
        if source.count(_TRACEBACK_SUPPRESS) != 1:
            raise SystemExit(
                "fastmcp/utilities/logging.py traceback suppression changed shape"
            )
        source = source.replace(
            _TRACEBACK_SUPPRESS,
            f'for package in ("{_vendored("fastmcp")}", "{_vendored("mcp")}", "pydantic")',
        )
    source = _METADATA_VERSION_RE.sub(
        lambda m: f'"{pins[_DISTRIBUTION_PIN.get(m.group(1), m.group(1))]}"', source
    )
    return _LOGGER_NAME_RE.sub(
        lambda m: (
            f'{m.group(1)}({m.group(2) or ""}__name__.removeprefix("{VENDOR_PREFIX}."))'
        ),
        source,
    )


def runtime_requirements(wheel_payload: bytes, extras: tuple[str, ...]) -> list[str]:
    """Base requirements plus those of ``extras``, with the extra marker removed."""
    with zipfile.ZipFile(io.BytesIO(wheel_payload)) as wheel:
        metadata_name = next(
            n for n in wheel.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)
        )
        metadata = wheel.read(metadata_name).decode("utf-8")
    requirements: list[str] = []
    for line in metadata.splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        requirement = line.removeprefix("Requires-Dist:").strip()
        spec, _, marker = requirement.partition(";")
        extra = re.search(r"""extra\s*==\s*["']([^"']+)["']""", marker)
        if extra is None:
            requirements.append(requirement)
        elif extra.group(1) in extras:
            # The extra clause may sit at either end of the conjunction.
            rest = re.sub(
                r"""(\s*\band\s+)?extra\s*==\s*["'][^"']+["'](\s+and\b\s*)?""",
                "",
                marker,
            ).strip()
            requirements.append(f"{spec.strip()}; {rest}" if rest else spec.strip())
    return sorted(dict.fromkeys(requirements))


_PROJECT_DEPENDENCIES_RE = re.compile(
    r"^\[project\]$.*?^dependencies = \[$(?P<body>.*?)^\]$", re.MULTILINE | re.DOTALL
)
_REQUIREMENT_RE = re.compile(
    r"\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?"
    r"(?P<specs>[^;]*)(?:;.*)?",
    re.DOTALL,
)
_SPECIFIER_RE = re.compile(r"(===|==|!=|~=|>=|<=|>|<)\s*(\S+)")


def _release(version: str) -> tuple[int, ...] | None:
    """Comparable release segments of a plain ``X.Y.Z`` version, else None."""
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        return None
    parts = [int(part) for part in version.split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _specifiers(specs: str) -> list[tuple[str, str]] | None:
    parsed: list[tuple[str, str]] = []
    for raw in filter(None, (s.strip() for s in specs.split(","))):
        if (match := _SPECIFIER_RE.fullmatch(raw)) is None:
            return None
        parsed.append((match.group(1), match.group(2)))
    return parsed


def _needed_floors(requirements: list[str]) -> dict[str, str]:
    floors: dict[str, str] = {}
    for requirement in requirements:
        match = _REQUIREMENT_RE.fullmatch(requirement)
        if match is None:
            continue
        name = canonical(match.group("name"))
        for operator, version in _specifiers(match.group("specs")) or []:
            release = _release(version)
            if operator != ">=" or release is None:
                continue
            current = floors.get(name)
            if current is None or release > (_release(current) or ()):
                floors[name] = version
    return floors


def _raise_floor(entry: str, floors: dict[str, str]) -> str:
    """``entry`` with its ``>=`` floor raised to the vendored packages' need.

    Only a ``>=`` floor below the need, under a ceiling that still admits it,
    is raised. Anything else (an exact pin, a ceiling the need passes, an
    unparsed version) is left for test_vendored_fastmcp.py to report.
    """
    match = _REQUIREMENT_RE.fullmatch(entry)
    needed = floors.get(canonical(match.group("name"))) if match else None
    specifiers = _specifiers(match.group("specs")) if match else None
    if match is None or needed is None or specifiers is None:
        return entry
    target = _release(needed)
    lows = [version for operator, version in specifiers if operator == ">="]
    if (
        target is None
        or len(lows) != 1
        or any(op not in {">=", "<", "<=", "!="} for op, _ in specifiers)
    ):
        return entry
    low = _release(lows[0])
    if low is None or low >= target:
        return entry
    for operator, version in specifiers:
        if operator in {"<", "<="}:
            top = _release(version)
            if top is None or target > top or (operator == "<" and target == top):
                return entry
    specs = re.sub(
        rf">=\s*{re.escape(lows[0])}(?![\w.])",
        f">={needed}",
        match.group("specs"),
        count=1,
    )
    return entry[: match.start("specs")] + specs + entry[match.end("specs") :]


def raise_dependency_floors(pyproject: str, requirements: list[str]) -> str:
    """Raise ``[project].dependencies`` floors the vendored packages outgrew.

    ha-mcp declares the vendored packages' requirements itself, and an install
    into Home Assistant keeps any version that meets the published floor, so a
    floor left behind keeps the older release installed.
    """
    match = _PROJECT_DEPENDENCIES_RE.search(pyproject)
    if match is None:
        raise SystemExit("no [project] dependencies array in pyproject.toml")
    floors = _needed_floors(requirements)
    body = re.sub(
        r'^(\s*")([^"]+)(")',
        lambda m: m.group(1) + _raise_floor(m.group(2), floors) + m.group(3),
        match.group("body"),
        flags=re.MULTILINE,
    )
    return pyproject[: match.start("body")] + body + pyproject[match.end("body") :]


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        payload: bytes = response.read()
    return payload


def _download_wheel(distribution: str, version: str) -> bytes:
    release = json.loads(
        _download(_PYPI_JSON_URL.format(name=distribution, version=version))
    )
    for entry in release.get("urls", []):
        if entry.get("packagetype") == "bdist_wheel" and str(
            entry.get("filename", "")
        ).endswith("-py3-none-any.whl"):
            print(f"downloading {entry['url']}")
            payload = _download(entry["url"])
            if hashlib.sha256(payload).hexdigest() != entry.get("digests", {}).get(
                "sha256"
            ):
                raise SystemExit(f"sha256 mismatch for {entry['filename']}")
            return payload
    raise SystemExit(f"no pure-Python wheel published for {distribution}=={version}")


def _license_from_wheel(payload: bytes) -> bytes | None:
    with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
        for name in wheel.namelist():
            parts = name.split("/")
            if parts[0].endswith(".dist-info") and parts[-1] in {
                "LICENSE",
                "LICENSE.txt",
            }:
                return wheel.read(name)
    return None


def stage_package(
    spec: VendoredPackage,
    wheel_payload: bytes,
    license_text: bytes,
    pins: dict[str, str],
    staging: Path,
) -> int:
    prefix = f"{spec.package}/"
    extracted = 0
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(wheel_payload)) as wheel:
        for name in wheel.namelist():
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            relative = name[len(prefix) :]
            if "__pycache__" in relative.split("/"):
                continue
            destination = (staging / relative).resolve()
            if not destination.is_relative_to(staging.resolve()):  # CWE-22
                raise SystemExit(
                    f"refusing archive member escaping the vendor dir: {name}"
                )
            data = wheel.read(name)
            if relative.endswith(".py"):
                source = patch_package(
                    spec.package, relative, rewrite_imports(data.decode("utf-8")), pins
                )
                if leftover := remaining_vendored_imports(source):
                    raise SystemExit(f"{name}: unrewritten imports {leftover}")
                if lookup := _UNPATCHED_LOOKUP_RE.search(source):
                    raise SystemExit(
                        f"{name}: unpatched package lookup {lookup.group(0)!r}"
                    )
                data = source.encode("utf-8")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            extracted += 1
    if not (staging / "__init__.py").is_file():
        raise SystemExit(f"wheel layout unexpected: no {spec.package}/__init__.py")
    (staging / "LICENSE").write_bytes(license_text)
    (staging / "VENDORED").write_text(
        f"{spec.distribution}=={pins[spec.distribution]}\n"
        "Vendored by scripts/vendor_fastmcp.py — do not edit by hand.\n",
        encoding="utf-8",
        newline="\n",
    )
    (staging / REQUIRES_NAME).write_text(
        "\n".join(runtime_requirements(wheel_payload, spec.extras)) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_manifest(staging)
    return extracted


def _promote(staged: dict[Path, Path]) -> None:
    """Swap all staged trees in; on failure restore every tree already swapped."""
    swapped: list[tuple[Path, Path, bool]] = []
    try:
        for target, staging in staged.items():
            backup = target.with_name(target.name + ".previous")
            shutil.rmtree(backup, ignore_errors=True)
            had_previous = target.exists()
            if had_previous:
                target.rename(backup)
            swapped.append((target, backup, had_previous))
            staging.rename(target)
    except BaseException:
        for target, backup, had_previous in reversed(swapped):
            shutil.rmtree(target, ignore_errors=True)
            if had_previous:
                backup.rename(target)
        for staging in staged.values():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    for _target, backup, _had in swapped:
        shutil.rmtree(backup, ignore_errors=True)


def _sync_pyproject(vendor_dir: Path, pyproject: Path) -> None:
    requirements = [
        line
        for spec in PACKAGES
        for line in (vendor_dir / spec.package / REQUIRES_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    current = pyproject.read_text(encoding="utf-8")
    raised = raise_dependency_floors(current, requirements)
    if raised != current:
        pyproject.write_text(raised, encoding="utf-8", newline="\n")
        print(f"raised dependency floors in {pyproject.name}")


def main(vendor_dir: Path = _VENDOR_DIR, pyproject: Path = _PYPROJECT) -> int:
    pins = read_pins(vendor_dir / "requirements.txt")
    staged: dict[Path, Path] = {}
    counts: dict[str, int] = {}
    try:
        for spec in PACKAGES:
            version = pins[spec.distribution]
            target = vendor_dir / spec.package
            staging = target.with_name(target.name + ".incoming")
            shutil.rmtree(staging, ignore_errors=True)
            staged[target] = staging
            wheel = _download_wheel(spec.distribution, version)
            license_text = _license_from_wheel(
                _download_wheel(spec.license_distribution, version)
                if spec.license_distribution
                else wheel
            )
            if license_text is None:
                raise SystemExit(f"no LICENSE found for {spec.distribution}=={version}")
            counts[spec.package] = stage_package(
                spec, wheel, license_text, pins, staging
            )
    except BaseException:
        for staging in staged.values():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    _promote(staged)
    _sync_pyproject(vendor_dir, pyproject)
    for spec in PACKAGES:
        print(
            f"vendored {spec.distribution}=={pins[spec.distribution]}: "
            f"{counts[spec.package]} files"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
