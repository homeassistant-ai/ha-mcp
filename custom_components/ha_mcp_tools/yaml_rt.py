"""ruamel.yaml round-trip helpers preserving comments and HA custom tags."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import date, datetime
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
    "!include_dir_merge_named",
    "!secret",
    "!env_var",
)


def _make_tag_constructor(tag: str) -> Callable[[Any, Any], _TaggedScalar]:
    """Return a ruamel.yaml constructor function for *tag*."""

    def _construct(loader: Any, node: Any) -> _TaggedScalar:
        return _TaggedScalar(tag, loader.construct_scalar(node))

    return _construct


def _represent_tagged_scalar(dumper: Any, data: _TaggedScalar) -> Any:
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


# Effectively-infinite emitter line width. ruamel's default (~80 columns)
# re-wraps long lines on dump; inside a ``>`` folded scalar a new wrap
# adjacent to a more-indented line becomes a LITERAL newline on re-parse,
# silently corrupting string literals in blocks an edit never touched
# (#1720). Never introducing new wraps also keeps untouched long lines
# byte-stable across edits.
_NEVER_WRAP_WIDTH = 2**31


# A top-level mapping key: starts at column 0, `key:` with nothing (or a
# comment) after the colon. Quoted/exotic keys never match — detection
# then just falls back to the default style, which is safe.
_TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z0-9_][^\s:]*:\s*(?:#.*)?$")
_DASH_RE = re.compile(r"^( *)- ")

# ruamel's compact defaults for block sequences (dash at the parent
# column). Used to RESET the shared per-thread instance between dumps.
_DEFAULT_SEQ_STYLE = (2, 0)


def detect_seq_indent(text: str) -> tuple[int, int] | None:
    """Detect the file's top-level block-sequence style.

    Returns ``(sequence, offset)`` for ``YAML.indent()`` — derived from
    the first list item that directly follows a top-level key — or
    ``None`` when the file has no such sequence. Only top-level
    sequences discriminate: nested dashes are indented in BOTH styles.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _TOP_LEVEL_KEY_RE.match(line):
            continue
        for nxt in lines[i + 1 :]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            m = _DASH_RE.match(nxt)
            if m:
                offset = len(m.group(1))
                return (offset + 2, offset)
            break  # value is not a sequence — try the next top-level key
    return None


def apply_seq_indent(ry: YAML, style: tuple[int, int] | None) -> None:
    """Apply a detected sequence style (or the compact default) to *ry*.

    ``make_yaml()`` instances are cached per-thread, so the style MUST be
    (re)applied before every dump — passing ``None`` resets to the
    default instead of leaking the previous file's style.
    """
    sequence, offset = style if style is not None else _DEFAULT_SEQ_STYLE
    ry.indent(mapping=2, sequence=sequence, offset=offset)


def _build_yaml() -> YAML:
    """Create a fresh round-trip YAML instance with HA tag support."""
    ry = YAML(typ="rt")
    ry.preserve_quotes = True
    ry.width = _NEVER_WRAP_WIDTH
    return ry


class _YAMLStorage(threading.local):
    """Thread-local storage for ruamel.yaml instances."""

    def __init__(self) -> None:
        self.yaml = _build_yaml()


_STORAGE = _YAMLStorage()


def make_yaml() -> YAML:
    """Return a round-trip YAML instance with HA tag support.

    The instance is cached per-thread to prevent ruamel.yaml from performing
    expensive plugin discovery (glob/scandir) on every call, which
    causes CPU spikes and event loop blocking during bulk edits.

    Thread-local storage is used because ruamel.yaml instances are not
    thread-safe.
    """
    try:
        return _STORAGE.yaml
    except AttributeError:
        _STORAGE.yaml = _build_yaml()
        return _STORAGE.yaml


def yaml_dumps(ry: YAML, data: Any) -> str:
    """Dump *data* to a string using the given YAML instance."""
    buf = StringIO()
    ry.dump(data, buf)
    return buf.getvalue()


def yaml_jsonify(node: Any) -> Any:
    """Convert a round-trip node into JSON-serializable plain Python.

    An HA tag is rendered back to its SOURCE form (``!secret api_key``), never
    resolved: the value behind a ``!secret`` lives in secrets.yaml and is not
    looked up here, so a parsed view carries no plaintext-secret surface — the
    same property the round-trip text view has. Lives here because this module
    owns ``_TaggedScalar`` and the tag registry.

    ruamel's scalar types subclass the builtins (``ScalarInt``/``ScalarFloat``/
    ``ScalarString``), so they are narrowed to the plain type; timestamps
    (``!!timestamp``) come back as ``date``/``datetime``, which json cannot
    encode, and become ISO strings.
    """
    if isinstance(node, _TaggedScalar):
        return f"{node.tag} {node.value}".strip()
    if isinstance(node, dict):
        return {str(key): yaml_jsonify(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [yaml_jsonify(item) for item in node]
    # bool before int: bool subclasses int.
    if node is None or isinstance(node, bool):
        return node
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    if isinstance(node, (datetime, date)):
        return node.isoformat()
    return str(node)
