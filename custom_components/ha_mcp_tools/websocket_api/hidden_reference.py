"""Whether a config record references a hidden entity, for an enforcing search.

An enforcing search (the wire's ``enforce``) omits the config records that
reference a hidden entity. The rules are the server's for a tool's output
(``ha_mcp.visibility.hidden_matcher``), so the search omits the same records
with and without the component, which cannot import the server package:

- A hidden entity_id counts wherever it stands, unless the longer word around
  it is another entity's own id. After a backslash the word is also judged
  without an escape's tail.
- A hidden entity's friendly name of several words counts as a phrase of its
  own, except where it is part of a visible entity's, a device's or an area's
  name.

Keys count as much as values: a scene keys its states by entity_id.
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .registry import _all_area_entries, _all_device_entries, _RegistryView

_WORD = frozenset(string.ascii_letters + string.digits + "_")
_ESCAPE_TAIL = re.compile(r"[bfnrt]|u[0-9a-f]{4}")
_ESCAPE_BEFORE = re.compile(r"\\(?:[bfnrt]|u[0-9a-f]{4})\Z", re.IGNORECASE)


def _words_around(text: str, first: int, last: int, reach: int) -> set[str]:
    """The word around ``text[first:last]``, followed at most ``reach`` either
    way; after a backslash, also that word without an escape's tail."""
    start, end = first, last
    while start > max(0, first - reach) and text[start - 1] in _WORD:
        start -= 1
    while end < min(len(text), last + reach) and text[end] in _WORD:
        end += 1
    word = text[start:end].lower()
    tail = _ESCAPE_TAIL.match(word) if text[start - 1 : start] == "\\" else None
    return {word, word[tail.end() :]} if tail else {word}


def _id_test(hidden: set[str], known: set[str]) -> Callable[[str], bool]:
    """A test for whether a text references one of the ``hidden`` entity_ids."""
    terms = {entity_id.lower() for entity_id in hidden}
    others = {entity_id.lower() for entity_id in known} - terms
    pattern = re.compile(
        "(?=({}))".format("|".join(re.escape(term) for term in sorted(terms))),
        re.IGNORECASE,
    )
    reach = max(map(len, terms | others)) + len("u0000") + 1

    def references(text: str) -> bool:
        for match in pattern.finditer(text):
            words = _words_around(text, *match.span(1), reach)
            if words & terms or not words & others:
                return True
        return False

    return references


def _fold(text: str) -> str:
    """``text`` lowercased one character at a time, so that its length holds
    where a letter's lowercase is longer than the letter."""
    return "".join(low if len(low := char.lower()) == 1 else char for char in text)


def _friendly_names(states: Iterable[Any]) -> dict[str, str]:
    """The friendly name of every entity whose state carries one."""
    names: dict[str, str] = {}
    for state in states:
        attributes = getattr(state, "attributes", None)
        name = attributes.get("friendly_name") if attributes else None
        if isinstance(name, str) and name.strip():
            names[state.entity_id] = name
    return names


def _place_names(view: _RegistryView) -> set[str]:
    """The device and area names, which a hidden entity's name may be part of."""
    rows: list[tuple[list[Any], tuple[str, ...]]] = [
        (_all_device_entries(view), ("name", "name_by_user")),
        (_all_area_entries(view), ("name",)),
    ]
    return {
        name
        for entries, keys in rows
        for entry in entries
        for key in keys
        if isinstance(name := getattr(entry, key, None), str)
    }


def _scanned_names(
    hidden: set[str], names: Mapping[str, str], places: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """The hidden entities' names of several words, lowercased, each with the
    visible names that contain it. A single word ("Home") cannot be told from
    ordinary text."""
    visible = {
        _fold(name) for entity_id, name in names.items() if entity_id not in hidden
    }
    visible |= {_fold(place) for place in places}
    return {
        folded: tuple(longer for longer in visible if folded in longer)
        for entity_id in hidden
        if len((name := names.get(entity_id, "")).split()) > 1
        for folded in [_fold(name)]
    }


def _name_test(names: Mapping[str, tuple[str, ...]]) -> Callable[[str], bool]:
    """A test for whether a text carries one of the scanned ``names``."""
    if not names:
        return lambda text: False
    pattern = re.compile(
        "(?={})".format("|".join(re.escape(name) for name in sorted(names))),
        re.IGNORECASE,
    )
    sizes = sorted({len(name) for name in names})

    def stands_alone(text: str, folded: str, first: int) -> bool:
        """Whether a name stands at ``first`` as a phrase of its own, outside
        every visible name that contains it. ``folded`` is ``text`` folded."""
        for last in (first + size for size in sizes):
            name = folded[first:last]
            if name not in names or text[last : last + 1].isalnum():
                continue
            if not any(
                folded.find(longer, max(0, last - len(longer)), first + len(longer))
                >= 0
                for longer in names[name]
            ):
                return True
        return False

    def carries(text: str) -> bool:
        folded = ""
        for match in pattern.finditer(text):
            first = match.start()
            before = text[max(0, first - 6) : first]
            if before[-1:].isalnum() and not _ESCAPE_BEFORE.search(before):
                continue
            # Folded once for the text, however many names stand in it.
            folded = folded or _fold(text)
            if stands_alone(text, folded, first):
                return True
        return False

    return carries


def _hidden_reference_test(
    view: _RegistryView, states: Iterable[Any], hidden: set[str], known: set[str]
) -> Callable[[Any], bool]:
    """A test for whether a config record references one of ``hidden``.

    ``known`` are the entity_ids Home Assistant has, ``states`` its states.
    """
    names_id = _id_test(hidden, known)
    carries_name = _name_test(
        _scanned_names(hidden, _friendly_names(states), _place_names(view))
    )

    def references(value: Any) -> bool:
        if isinstance(value, str):
            return names_id(value) or carries_name(value)
        if isinstance(value, Mapping):
            return any(references(k) or references(v) for k, v in value.items())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(references(item) for item in value)
        return False

    return references
