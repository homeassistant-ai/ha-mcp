"""How the enforce-mode scans find a hidden entity_id in text (issue #3156).

In a tool's output a hidden id counts wherever it stands: alone, in a dotted
path (``states.sensor.foo``), or run together with other characters
(``12sensor.foo``, ``sensor.foo99``). The one exception is a longer word that
is another entity's own id (``binary_sensor.foo``, ``sensor.foo_2``).

In a call's arguments only the id as a whole word counts. A longer word there
is an identifier the caller typed, and refusing it would tell the caller that
a hidden id is part of it.

Scanned text is often serialized, so a word may begin with what an escape
leaves after its backslash (the ``n`` of a newline's ``\\n``). The word is
judged with and without that tail.

A hidden entity's friendly name counts in output too, as a phrase of its own
(``Front Door Lock Battery | 87``), so that a list of names and states is
refused like a list of ids. A name does not count where it is, or is part of, a
visible entity's, a device's or an area's name: a lock named after its device
would otherwise close every listing of that device. A name of a single word
("Home") is not scanned for, as it cannot be told from ordinary text. A state alone (``87``) names nothing.
"""

from __future__ import annotations

import json
import re
import string
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .identifier_aliases import IdentifierAliases

_WORD = frozenset(string.ascii_letters + string.digits + "_")
_ESCAPE_TAIL = re.compile(r"[bfnrt]|u[0-9a-f]{4}")
_ESCAPE_BEFORE = re.compile(r"\\(?:[bfnrt]|u[0-9a-f]{4})\Z", re.IGNORECASE)


def iter_strings(value: Any) -> Any:
    """Yield every string in a nested args/result structure (dicts/lists/strings).

    Dict KEYS are yielded too: HA config payloads key maps by entity_id (a scene's
    ``entities``, a group's members), so a key is as much a reference as a value —
    and the outbound scan (``json.dumps``) sees keys, so the inbound scan must
    traverse the same surface or a hidden id positioned as a key would execute.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_strings(item)


def known_entity_ids(states: Any, registry: Any) -> set[str]:
    """Every entity_id Home Assistant has a state or a registry row for."""
    rows = list(states) if isinstance(states, list) else []
    entries = registry.get("result") if isinstance(registry, dict) else None
    if isinstance(entries, list):
        rows += entries
    return {
        row["entity_id"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("entity_id"), str)
    }


def friendly_names(states: Any) -> dict[str, str]:
    """The friendly name of every entity whose state carries one."""
    names: dict[str, str] = {}
    for state in states if isinstance(states, list) else []:
        attributes = state.get("attributes") if isinstance(state, dict) else None
        name = attributes.get("friendly_name") if isinstance(attributes, dict) else None
        if isinstance(name, str) and name.strip():
            names[state["entity_id"]] = name
    return names


def _fold(text: str) -> str:
    """``text`` lowercased one character at a time, so that its length holds
    where a letter's lowercase is longer than the letter."""
    return "".join(low if len(low := char.lower()) == 1 else char for char in text)


def _name_forms(name: str) -> set[str]:
    """``name`` lowercased: as it is, and as a JSON string writes it and its
    lowercase."""
    written = (json.dumps(text)[1:-1] for text in (name, _fold(name)))
    return {_fold(name), *map(_fold, written)}


def _read_back(text: str) -> str:
    """``text`` as a JSON string holding it reads, or as it is."""
    try:
        return str(json.loads(f'"{text}"'))
    except ValueError:
        return text


def _plain(form: str) -> str:
    """What every form of one name comes to: read back and lowercased."""
    return _fold(_read_back(form))


def _typed_forms(text: str) -> set[str]:
    """The forms of an argument ``text``, also read back from a JSON string,
    so either form there stands for both in the output."""
    return _name_forms(text) | _name_forms(_read_back(text))


def _scanned_names(
    hidden: set[str], names: Mapping[str, str], places: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """The hidden entities' names of several words, each with the visible
    names that contain it. A single word ("Home") cannot be told from
    ordinary text."""
    visible = {
        form
        for name in [n for e, n in names.items() if e not in hidden] + list(places)
        for form in _name_forms(name)
    }
    everything = "\n".join(visible)
    return {
        form: tuple(v for v in visible if form in v) if form in everything else ()
        for entity_id in hidden
        if len((name := names.get(entity_id, "")).split()) > 1
        for form in _name_forms(name)
    }


class _Places:
    """Where the longer names stand in one scanned text, each found once."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._folded = ""
        self._starts: dict[str, list[int]] = {}

    def within(self, first: int, last: int, longer: str) -> bool:
        """Whether ``longer`` stands in the text over all of ``first:last``."""
        starts = self._starts.get(longer)
        if starts is None:
            self._folded = self._folded or _fold(self._text)
            starts = self._starts[longer] = []
            at = self._folded.find(longer)
            while at >= 0:
                starts.append(at)
                at = self._folded.find(longer, at + 1)
        nearest = bisect_right(starts, first) - 1
        return nearest >= 0 and starts[nearest] + len(longer) >= last


@dataclass(frozen=True)
class HiddenMatcher:
    """The enforced hidden set, its identifier aliases and what its scans match.

    ``identifier_aliases`` holds the bare keys that address a hidden script,
    scene or automation (see ``identifier_aliases``). ``terms`` are the hidden
    entity_ids plus the alias terms that run a hidden script without naming it,
    and ``other_ids`` the ids of every other entity. ``names`` are the hidden
    entities' scanned friendly names, lowercased, each also as a JSON string
    writes it, with the visible names that contain it. Built only through :meth:`from_hidden`, so ``regex`` is ``None``
    exactly when ``hidden`` is empty and every scan sees one consistent view.
    """

    hidden: frozenset[str]
    identifier_aliases: IdentifierAliases = field(hash=False)
    terms: frozenset[str] = field(hash=False)
    other_ids: frozenset[str] = field(hash=False)
    regex: re.Pattern[str] | None = field(hash=False)
    longest: int = field(hash=False)
    names: Mapping[str, tuple[str, ...]] = field(hash=False)
    name_regex: re.Pattern[str] | None = field(hash=False)

    @classmethod
    def from_hidden(
        cls,
        hidden: set[str],
        aliases: IdentifierAliases,
        known_ids: set[str],
        names: Mapping[str, str] | None = None,
        places: Iterable[str] = (),
    ) -> HiddenMatcher:
        """``names`` are the entities' friendly names and ``places`` the device
        and area names; without ``names`` no name is scanned for."""
        terms = frozenset(hidden | aliases.embedded_terms())
        alternation = "|".join(re.escape(term) for term in sorted(terms))
        scanned = _scanned_names(hidden, names or {}, places)
        phrases = "|".join(re.escape(name) for name in sorted(scanned))
        return cls(
            frozenset(hidden),
            aliases,
            terms,
            frozenset(known_ids) - terms,
            re.compile(f"(?=({alternation}))", re.IGNORECASE) if terms else None,
            max(map(len, terms | known_ids), default=0),
            scanned,
            re.compile(f"(?={phrases})", re.IGNORECASE) if scanned else None,
        )

    def exact(self, text: str) -> bool:
        """Whether ``text`` is a hidden entity_id."""
        return text in self.hidden

    def embeds(self, text: str) -> bool:
        """Whether output ``text`` embeds a hidden entity_id or friendly name."""
        return any(
            not self.terms.isdisjoint(words) or self.other_ids.isdisjoint(words)
            for words in self._words(text)
        ) or any(self._named(text))

    def _named(self, text: str) -> Iterator[str]:
        """The scanned names that stand in ``text`` as a phrase of their own.

        No letter or digit may run into the phrase, an escape's tail before it
        aside (the ``n`` of ``\\n``). A name does not count where it is part
        of a visible entity's, a device's or an area's name.
        """
        if self.name_regex is None:
            return
        sizes: list[int] = []
        places = _Places(text)
        for match in self.name_regex.finditer(text):
            first = match.start()
            before = text[max(0, first - 6) : first]
            if before[-1:].isalnum() and not _ESCAPE_BEFORE.search(before):
                continue
            # Every name that starts here is tried, a shorter one too.
            sizes = sizes or sorted({len(name) for name in self.names})
            here = _fold(text[first : first + sizes[-1]])
            for last in (first + size for size in sizes):
                if last > len(text):
                    break  # the text ends first; a clipped slice is no name
                name = here[: last - first]
                if name not in self.names or text[last : last + 1].isalnum():
                    continue
                if not any(places.within(first, last, n) for n in self.names[name]):
                    yield name

    def unknown_word(self, text: str) -> bool:
        """Whether ``text`` holds a longer word around a hidden id no entity has."""
        return any(
            self.terms.isdisjoint(words) and self.other_ids.isdisjoint(words)
            for words in self._words(text)
        )

    def addresses(self, text: str) -> bool:
        """Whether argument ``text`` names a hidden entity_id as a whole word."""
        return any(not self.terms.isdisjoint(words) for words in self._words(text))

    def for_call(self, args: Any) -> HiddenMatcher:
        """This matcher for the output of the call with ``args``.

        A longer word the arguments hold is the caller's own text when the
        result or an error echoes it, so it counts like another entity's id.
        An argument that holds a name counts like a visible name containing
        it: the name is the caller's own text where the argument is repeated,
        and nowhere else in the output. An argument that is the name alone
        is repeated wherever the name stands; its caller knew the name.
        """
        typed = {
            word
            for text in iter_strings(args)
            for words in self._words(text)
            if self.terms.isdisjoint(words)
            for word in words
        }
        held: dict[str, set[str]] = {}
        for text in iter_strings(args):
            for name in self._named(text):
                held.setdefault(_plain(name), set()).update(_typed_forms(text))
        if not typed and not held:
            return self
        return replace(
            self,
            other_ids=self.other_ids | typed,
            names={
                name: (*longer, *held.get(_plain(name), ())) if held else longer
                for name, longer in self.names.items()
            },
        )

    def _words(self, text: str) -> Iterator[list[str]]:
        """The word around each matched term, with and without an escape's tail.

        Terms are matched where they overlap too: in ``binary_sensor.light.kitchen``
        the hidden ``light.kitchen`` starts inside ``sensor.light``. A word is
        followed only a little past the longest id: longer, it is no entity's
        id, and a long run of word characters is not walked again for every
        term in it.
        """
        if self.regex is None:
            return
        reach = self.longest + len("u0000") + 1
        for match in self.regex.finditer(text):
            first, last = match.span(1)
            start, end = first, last
            while start > max(0, first - reach) and text[start - 1] in _WORD:
                start -= 1
            while end < min(len(text), last + reach) and text[end] in _WORD:
                end += 1
            word = text[start:end].lower()
            tail = _ESCAPE_TAIL.match(word) if text[start - 1 : start] == "\\" else None
            yield [word, word[tail.end() :]] if tail else [word]


def scan_value(value: Any, matcher: HiddenMatcher) -> tuple[str | None, bool]:
    """Return ``(first_exact_hidden_id, any_embedded_hidden_id)`` over nested strings.

    A string that equals a hidden id exactly drives concealment; a string that
    merely embeds one as a whole word drives the generic refusal. An exact
    match takes priority, so an exact hit never also counts as embedded.
    """
    exact: str | None = None
    embedded = False
    for text in iter_strings(value):
        if matcher.exact(text):
            if exact is None:
                exact = text
        elif matcher.addresses(text):
            embedded = True
    return exact, embedded
