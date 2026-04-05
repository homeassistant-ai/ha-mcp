"""ruamel.yaml round-trip helpers preserving comments and HA custom tags."""

from __future__ import annotations

from io import StringIO
from typing import Any

from ruamel.yaml import YAML


class _TaggedScalar:
    """Wrapper that stores a YAML tag + scalar value for lossless round-trip."""

    __slots__ = ("tag", "value")

    def __init__(self, tag: str, value: str) -> None:
        self.tag = tag
        self.value = value

    def __repr__(self) -> str:
        return f"_TaggedScalar({self.tag!r}, {self.value!r})"

    def __str__(self) -> str:
        return self.value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _TaggedScalar):
            return NotImplemented
        return self.tag == other.tag and self.value == other.value

    def __hash__(self) -> int:
        return hash((self.tag, self.value))


_HA_TAGS = (
    "!include",
    "!include_dir_list",
    "!include_dir_named",
    "!include_dir_merge_list",
    "!secret",
    "!env_var",
)


def _make_tag_constructor(tag: str):
    """Return a ruamel.yaml constructor function for *tag*."""

    def _construct(loader, node):
        return _TaggedScalar(tag, loader.construct_scalar(node))

    return _construct


def _represent_tagged_scalar(dumper, data: _TaggedScalar):
    """Representer that emits the original tag + scalar value."""
    return dumper.represent_scalar(data.tag, data.value)


def _register_ha_tags() -> None:
    """Register HA tag constructors/representers on the shared class registries.

    ``add_constructor`` / ``add_representer`` mutate class-level registries
    shared by all ``YAML(typ="rt")`` instances.  We call this once at import
    time; ``make_yaml()`` then only creates a fresh (thread-safe) instance.
    """
    # Use a temporary instance to access the Constructor/Representer classes
    _tmp = YAML(typ="rt")
    for tag in _HA_TAGS:
        _tmp.Constructor.add_constructor(tag, _make_tag_constructor(tag))
    _tmp.Representer.add_representer(_TaggedScalar, _represent_tagged_scalar)


_register_ha_tags()


def make_yaml() -> YAML:
    """Return a fresh round-trip YAML instance with HA tag support."""
    ry = YAML(typ="rt")
    ry.preserve_quotes = True
    return ry


def yaml_dumps(ry: YAML, data: Any) -> str:
    """Dump *data* to a string using the given YAML instance."""
    buf = StringIO()
    ry.dump(data, buf)
    return buf.getvalue()
