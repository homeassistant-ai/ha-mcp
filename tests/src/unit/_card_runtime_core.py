"""The Python packages a Home Assistant install has, for the optional QuickJS runtime.

``async_ensure_runtime`` reads which QuickJS provider is installed, what other
installed packages require of it, and Core's package constraints. The unit
environment has its own provider, so these helpers give it the install a test
describes: real ``importlib.metadata`` distributions read from ``.dist-info``
folders, and a Core package directory holding the constraints.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from importlib import metadata
from importlib.machinery import ModuleSpec
from pathlib import Path

import homeassistant
import pytest
from packaging.utils import canonicalize_name

__all__ = ["constrained_core", "installed_packages"]

_PROVIDERS = ("quickjs", "quickjs-ng")


def _distribution(
    root: Path, name: str, version: str, requires: Sequence[str]
) -> metadata.Distribution:
    folder = root / f"{name.replace('-', '_')}-{version}.dist-info"
    folder.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    (folder / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metadata.Distribution.at(folder)


@contextmanager
def installed_packages(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    packages: Mapping[str, str],
    *,
    requires: Sequence[str] = (),
) -> Iterator[dict[str, str]]:
    """Only ``packages`` (name: version) provide QuickJS, and the install holds
    one more package that ``requires`` these requirements. The yielded dict is
    the providers installed now: a test adds one to install it."""
    dists = [
        _distribution(root, name, version, ()) for name, version in packages.items()
    ]
    dists.append(_distribution(root, "another-package", "1.0", requires))
    versions: dict[str, str] = {
        str(canonicalize_name(name)): version for name, version in packages.items()
    }
    real_version, real_find_spec = metadata.version, importlib.util.find_spec

    def version(name: str) -> str:
        key = canonicalize_name(name)
        if key in versions:
            return versions[key]
        if key in _PROVIDERS:
            raise metadata.PackageNotFoundError(name)
        return real_version(name)

    def find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
        if name == "quickjs":
            return ModuleSpec(name, None) if set(_PROVIDERS) & set(versions) else None
        return real_find_spec(name, package)

    with monkeypatch.context() as patch:
        patch.setattr(metadata, "version", version)
        patch.setattr(metadata, "distributions", lambda: dists)
        patch.setattr(importlib.util, "find_spec", find_spec)
        yield versions


def constrained_core(
    monkeypatch: pytest.MonkeyPatch, root: Path, constraints: str
) -> None:
    """Home Assistant's package constraints are ``constraints``."""
    (root / "package_constraints.txt").write_text(constraints, encoding="utf-8")
    monkeypatch.setattr(homeassistant, "__file__", str(root / "__init__.py"))
