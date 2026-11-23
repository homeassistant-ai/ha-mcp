"""The allowlist of ``scripts/integration_stand_ins.py``: the stand-ins kept
under a real integration's name, each with its file and the reason.

``scripts/stand_in_lists_ratchet.py`` reads the list with this reader too, so an
entry the check refuses fails there as well.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ALLOWLIST = "tests/src/unit/integration_stand_in_allowlist.yml"
ENTRY_KEYS = ("file", "integration", "reason")
# A domain; this also keeps the check's own markers off the list.
_DOMAIN = re.compile("[a-z0-9_]+")


def load_allowlist(path: Path, problems: list[str]) -> dict[tuple[str, str], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        problems.append(f"{ALLOWLIST}: cannot be read: {err}")
        return {}
    return parse_allowlist(text, problems)


def _repeated_key(
    node: yaml.Node | None, above: frozenset[int] = frozenset()
) -> yaml.ScalarNode | None:
    """A key repeated in one mapping, which loading would settle silently by
    keeping the last value. A node that holds itself through an alias is not
    a list of entries."""
    if id(node) in above:
        raise yaml.YAMLError("an alias makes the list hold itself")
    above |= {id(node)}
    if isinstance(node, yaml.MappingNode):
        keys = [key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
        for index, (key, _) in enumerate(node.value):
            if isinstance(key, yaml.ScalarNode) and key.value in keys[:index]:
                return key
        children = [value for _, value in node.value]
    elif isinstance(node, yaml.SequenceNode):
        children = list(node.value)
    else:
        return None
    found = (_repeated_key(child, above) for child in children)
    return next((key for key in found if key is not None), None)


def parse_allowlist(text: str, problems: list[str]) -> dict[tuple[str, str], str]:
    """Each entry's reason by (file, integration). A bad entry is a problem, and
    is still returned when it names both, so a reader comparing two versions of
    the list sees every file on each."""
    try:
        repeated = _repeated_key(yaml.compose(text, Loader=yaml.SafeLoader))
        entries = yaml.safe_load(text)
    except yaml.YAMLError as err:
        problems.append(f"{ALLOWLIST}: cannot be read: {err}")
        return {}
    if repeated is not None:
        line = repeated.start_mark.line + 1
        problems.append(f"{ALLOWLIST}:{line}: repeats the key {repeated.value!r}")
    if entries is None:  # an empty file lists nothing
        entries = []
    if not isinstance(entries, list):
        problems.append(f"{ALLOWLIST}: must be a list of entries")
        return {}
    allowed: dict[tuple[str, str], str] = {}
    for number, entry in enumerate(entries, 1):
        fields = entry if isinstance(entry, dict) else {}
        file, integration, reason = (fields.get(key) for key in ENTRY_KEYS)
        if set(fields) != set(ENTRY_KEYS) or not all(
            isinstance(value, str) and value for value in (file, integration, reason)
        ):
            problems.append(
                f"{ALLOWLIST}: entry {number} needs exactly a non-empty "
                + ", ".join(ENTRY_KEYS)
            )
        elif not _DOMAIN.fullmatch(str(integration)):
            problems.append(
                f"{ALLOWLIST}: entry {number} names no integration: {integration!r}"
            )
        if not (isinstance(file, str) and isinstance(integration, str)):
            continue
        key = (file, integration)
        if key in allowed:
            problems.append(f"{ALLOWLIST}: entry {number} repeats {key[0]} {key[1]}")
        allowed[key] = reason if isinstance(reason, str) else ""
    return allowed
