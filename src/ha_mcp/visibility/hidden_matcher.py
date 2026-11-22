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
"""

from __future__ import annotations

import re
import string
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any

from .identifier_aliases import IdentifierAliases

_WORD = frozenset(string.ascii_letters + string.digits + "_")
_ESCAPE_TAIL = re.compile(r"[bfnrt]|u[0-9a-f]{4}")


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


@dataclass(frozen=True)
class HiddenMatcher:
    """The enforced hidden set, its identifier aliases and what its scans match.

    ``identifier_aliases`` holds the bare keys that address a hidden script,
    scene or automation (see ``identifier_aliases``). ``terms`` are the hidden
    entity_ids plus the alias terms that run a hidden script without naming it,
    and ``other_ids`` the ids of every other entity. Built only through
    :meth:`from_hidden`, so ``regex`` is ``None`` exactly when ``hidden`` is
    empty and every scan sees one consistent view.
    """

    hidden: frozenset[str]
    identifier_aliases: IdentifierAliases = field(hash=False)
    terms: frozenset[str] = field(hash=False)
    other_ids: frozenset[str] = field(hash=False)
    regex: re.Pattern[str] | None = field(hash=False)
    longest: int = field(hash=False)

    @classmethod
    def from_hidden(
        cls, hidden: set[str], aliases: IdentifierAliases, known_ids: set[str]
    ) -> HiddenMatcher:
        terms = frozenset(hidden | aliases.embedded_terms())
        alternation = "|".join(re.escape(term) for term in sorted(terms))
        return cls(
            frozenset(hidden),
            aliases,
            terms,
            frozenset(known_ids) - terms,
            re.compile(f"(?=({alternation}))", re.IGNORECASE) if terms else None,
            max(map(len, terms | known_ids), default=0),
        )

    def exact(self, text: str) -> bool:
        """Whether ``text`` is a hidden entity_id."""
        return text in self.hidden

    def embeds(self, text: str) -> bool:
        """Whether output ``text`` is, or embeds, a hidden entity_id."""
        return any(
            not self.terms.isdisjoint(words) or self.other_ids.isdisjoint(words)
            for words in self._words(text)
        )

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
        """
        typed = {
            word
            for text in iter_strings(args)
            for words in self._words(text)
            if self.terms.isdisjoint(words)
            for word in words
        }
        return replace(self, other_ids=self.other_ids | typed) if typed else self

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
