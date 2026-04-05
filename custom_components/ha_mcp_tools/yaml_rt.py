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


def make_yaml() -> YAML:
    """Return a round-trip YAML instance with HA tag support.

    Note: ``add_constructor`` / ``add_representer`` mutate the shared class
    registries, not per-instance state.  Re-registering is idempotent (dict
    overwrite) and harmless, but callers should not assume instance isolation.
    """
    ry = YAML(typ="rt")
    ry.preserve_quotes = True
    for tag in _HA_TAGS:
        ry.Constructor.add_constructor(tag, _make_tag_constructor(tag))
    ry.Representer.add_representer(_TaggedScalar, _represent_tagged_scalar)
    return ry


def yaml_dumps(ry: YAML, data: Any) -> str:
    """Dump *data* to a string using the given YAML instance."""
    buf = StringIO()
    ry.dump(data, buf)
    return buf.getvalue()
