"""The comparison a translation has to survive, in one place.

Two callers ask the same question and must not answer it differently. The
merge-time check in ``tests/src/unit/test_locale_parity.py`` asks it of the
catalogs already on disk; ``translate_locales._validate`` asks it of every
string the engine produces, before it is accepted.

Keeping one implementation is not tidiness. The CI check exists because the
sync will not revisit a key whose English still hashes the same, so a fault
that lands is frozen; the engine-side call exists so that the fault does not
land in the first place, because a partial run that trips the merge gate is
held back whole and re-planned the next day. Two copies of these rules would
mean the engine accepting exactly what the gate later refuses -- red every
morning, no forward progress, until someone hand-edits a catalog.

The two calls differ in one deliberate respect, spelled out on
``_parity_fault``: the engine does not compare number multisets.
"""

from __future__ import annotations

import re
from collections import Counter
from functools import cache

# A digit run preceded by a letter or another digit belongs to an identifier
# ("Z2M", "MQTT5"), not to a claim about quantity, so it is not a number a
# translation owes. A trailing unit is deliberately left out of the token:
# a locale that writes "5 тыс." for "5K" still matches on the digits, while
# "46K" where the English says 90% and 5K still reports as changed.
#
# Separators inside a number are kept as GROUP BOUNDARIES rather than deleted.
# Deleting them tolerates locale punctuation but also erases the difference
# between the "4.5:1" contrast ratio and "45:1", and between the minimum
# component version "1.2.4" and "12.4" -- both live English strings. (It is
# a floor a help string states, not what the component is on: that is 1.3.3.)
# Comparing the tuple
# of groups keeps "4.5" and "4,5" equal while "45" stays a different number.
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,   ]\d+)*")
# A magnitude suffix is part of the claim: "5K" and "5M" share their digits.
_MAGNITUDE_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)([KMGT])(?![A-Za-z])")
# A percentage is a unit every catalog keeps, spaced or not: four write
# "90 %" and four "90%", so the sign is required but the space is free.
_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)\s?%")
# A storage unit is part of the claim too: "1-256 MB" and "1-256 GB" differ by
# three orders of magnitude while the digits match.
_COMPOUND_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d+)\s*([A-Za-zА-Яа-я]{2})(?![A-Za-zА-Яа-я])"
)
_KNOWN_UNITS = frozenset({"KB", "MB", "GB", "TB"})
# A signed number, for the arm that compares what a translation added. The
# number is spelled by reference rather than repeated: a hand-copied group
# class lost the two non-breaking spaces a locale groups with, so "-1 000"
# was read as "-1" while the copy still looked right.
_SIGNED_NUMBER_RE = re.compile(rf"(?<![\w.,])([-+])({_NUMBER_RE.pattern})")
# ... and the dash between two range endpoints is not a sign, whatever the
# catalog put between the left endpoint and the dash. `\s` covers the
# non-breaking spaces these catalogs group with -- both are Unicode whitespace
# -- so no class is written out by hand, which is how the copy above lost them
# once already; a newline is excluded, because two endpoints do not straddle
# one. The optional unit or percent sign is there because a locale may repeat
# it on both bounds: "1 MB -256 MB", "50 % -100 %".
_RANGE_LEFT_CONTEXT_RE = re.compile(r"\d[^\S\n]*(?:%|[A-Za-zА-Яа-я]{1,3})?[^\S\n]*\Z")
# The same four units as every catalog spells them. Without this the filter
# that stops a localised spelling false-positiving also stops a *wrong*
# localised spelling reporting: "предел 1-256 ГБ" and "limite de 1-256 Go"
# both state gigabytes where the English says megabytes, and both passed.
# Russian transliterates the prefix and abbreviates "байт"; French sets "o"
# for "octet". Only spellings a shipped catalog actually uses are listed --
# an unknown two-letter token stays uncomparable, which is the safe arm.
_LOCALISED_UNITS = {
    "КБ": "KB",
    "МБ": "MB",
    "ГБ": "GB",
    "ТБ": "TB",
    "Ko": "KB",
    "Mo": "MB",
    "Go": "GB",
    "To": "TB",
}


def _canonical_unit(unit: str) -> str | None:
    """One storage unit as its English spelling, or None if not comparable."""
    if unit.upper() in _KNOWN_UNITS:
        return unit.upper()
    return _LOCALISED_UNITS.get(unit)


# "N > 0" and "N < 0" are opposite conditions with identical numbers.
_COMPARISON_RE = re.compile(r"([A-Za-z_]\w*)\s*([<>]=?)\s*(\d+)")
# A range is an ordered claim, and reversing it leaves the digits untouched:
# "Range 1-600" and "Range 600-1" hold the same multiset while the second
# names a lower bound above its upper one. The endpoints are spelled by
# reference to the number pattern rather than repeated, so a grouped bound
# ("1 024") keeps reading as one endpoint here too, and every dash a catalog
# might set counts -- hyphen through em dash. A ratio is the same ordered
# claim behind a different separator, and the corpus ships one: the "4.5:1"
# contrast ratio every catalog carries. Reversing it to "1:4,5" leaves the
# digits untouched exactly as a reversed range does, so a colon counts here
# too -- both the ASCII one and the fullwidth colon a CJK catalog may set.
_ORDERED_PAIR_RE = re.compile(
    rf"({_NUMBER_RE.pattern})\s*([-‐-―:：])\s*({_NUMBER_RE.pattern})(?![A-Za-z0-9])"
)
_GROUP_SEPARATOR_RE = re.compile(r"[.,   ]")

# Tokens a reader has to type, search for, or find on disk. Prose slashes
# ("read/write") are not paths, hence the requirement that a path start at a
# separator; a bare word is not an identifier, hence the required underscore.
# Files come FIRST inside the literal pattern below: with snake_case ahead of
# them, "tool_policy.json" tokenised as "tool_policy" plus ".json", and neither
# half is the name a reader has to find. "*" belongs in the stem for
# "packages/*.yaml". Spelled here rather than inline so the reverse check can
# ask for this shape alone without a second copy of it to keep in step.
_FILE_PATTERN = r"[\w.~*/-]*\.(?:yaml|yml|json|py|md|txt)"
_FILE_RE = re.compile(_FILE_PATTERN)

_LITERAL_RE = re.compile(
    rf"""(?:
        {_FILE_PATTERN}                         # files: configuration.yaml
      | [a-z][a-z0-9]*(?:_[a-z0-9]+)+           # snake_case: enable_tool_search
      | [A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+           # ALL_CAPS: DISABLED_TOOLS
      | (?<![\w/<])~?/[\w.*-]+(?:/[\w.*-]+)*    # paths: /api/settings/features
        # A hidden name has no extension and no leading separator, so neither
        # the file arm nor the path arm sees it: the deny floor the component
        # describes blocks ".storage", and a translation dropping it left no
        # trace. It sits after both so a name inside a path ("~/.ha-mcp/x") or
        # an extension ("packages/*.yaml") is still read whole -- those two
        # start earlier in the string and match there. A dotted continuation
        # belongs to the name: split off ".env" and a translation that keeps
        # ".env.local" intact reports the half as lost, because the boundary
        # rule refuses a name followed by another dotted segment.
      | (?<![\w./-])\.[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*(?!\w)  # .storage
      | [a-z][a-z0-9+.-]*://\S*                 # any scheme, bare one included
      | (?<![\w.])[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+(?![\w])  # group.set
      | (?<!\w)[A-Za-z][A-Za-z_]*\d+(?!\w)      # Jinja2, alert2
        # A repository slug, both halves hyphenated. That shape is what keeps
        # "read/write", "enable/disable" and "re-add/refresh" out -- prose
        # pairs a slash without hyphenating both sides. A slug that hyphenates
        # neither would slip through; none ships today.
      | (?<![\w/])[a-z0-9]+(?:-[a-z0-9]+)+/[a-z0-9]+(?:-[a-z0-9]+)+(?![\w/])
      | (?<!\w)[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+(?!\w)  # WebSocket, Z2M
    )""",
    re.X,
)

# An exact argument a caller has to pass: yaml_path='automation'. The word
# alone is ordinary prose a locale should translate, so only the quoted form
# counts -- and the parameter name is captured with it, because the value on
# its own says nothing about which argument it belongs to: "scope='snapshot',
# action='delete'" and "scope='delete', action='snapshot'" share both values
# and describe opposite calls.
_QUOTED_ASSIGNMENT_RE = re.compile(r"(?<![\w])([a-z_]+\s*=\s*'[^']+')")

# Latin abbreviations the dotted-identifier arm would otherwise claim. Prose,
# not names: no reader types "e.g" into anything.
_PROSE_ABBREVIATIONS = frozenset({"e.g", "i.e"})


def _canonical_number(token: str) -> tuple[str, ...]:
    """One number as its groups, with thousands grouping folded away.

    A separator followed by groups of exactly three digits is grouping, not a
    boundary: English ships `10000`, `1440` and `65535`, and a locale writing
    `10.000` states the same number. Anything else keeps its groups, so the
    "4.5:1" ratio and the minimum component version "1.2.4" stay distinct
    from "45" and "12.4".
    A decimal written to exactly three places is the ambiguous case and folds
    with the thousands; no English string has one.
    """
    groups = _GROUP_SEPARATOR_RE.split(token)
    if (
        len(groups) > 1
        and len(groups[0]) <= 3
        and all(len(group) == 3 for group in groups[1:])
    ):
        return ("".join(groups),)
    return tuple(groups)


def _numbers(text: str) -> Counter[tuple[str, ...]]:
    return Counter(_canonical_number(token) for token in _NUMBER_RE.findall(text))


def _lost_magnitudes(english: str, translated: str) -> list[str]:
    """Magnitude suffixes the translation contradicts rather than drops.

    A unit is part of the claim, and the value comparison cannot see it: "5K"
    and "5M" carry the same digits, and so do "90%" and a bare "90". The
    percent sign is required outright because every catalog keeps it -- four
    write "90 %" and four "90%", so only the space is free. Requiring a
    magnitude suffix outright would be wrong, though: Polish writes
    "5 tys." and Russian "5 тыс." for it, and both are correct. So the suffix
    is only compared when the translation itself puts a letter straight
    onto those digits -- six of the nine catalogs do, Polish and Russian spell
    it out, and the Italian value is one this branch deletes for the sync to
    rewrite.

    A storage unit is compared in the alphabet the catalog writes it in, not
    only in Latin: "предел 1-256 ГБ" and "limite de 1-256 Go" both state
    gigabytes where the English says megabytes, and while the filter that
    keeps a *correct* localised spelling from reporting was in place, so was
    the blind spot for a wrong one.

    Both suffix arms ask whether ANY occurrence of the digits carries the
    right unit, rather than settling on the first. The first occurrence is not
    necessarily the matching one -- "bis zu 5x schneller, etwa 5K Token" is
    faithful and reported, "jusqu'a 256 Go puis 256 MB" contradicts the
    English and did not.

    A sign is the same kind of claim and is compared in ``_invented_signs``.
    """
    contradicted = set()
    for digits, unit in _COMPOUND_UNIT_RE.findall(english):
        if unit.upper() not in _KNOWN_UNITS:
            continue
        # EVERY occurrence of these digits, not the first one: "jusqu'a 256 Go
        # puis 256 MB" carries the unit on its second, and settling on the
        # first reported a correct translation as a contradiction while
        # missing the storage arm's own case. One occurrence spelling the
        # unit right is the translation carrying the claim.
        carried = {
            canonical
            for match in re.finditer(
                rf"(?<![A-Za-z0-9]){re.escape(digits)}\s*([A-Za-zА-Яа-я]{{2}})"
                r"(?![A-Za-zА-Яа-я])",
                translated,
            )
            if (canonical := _canonical_unit(match.group(1))) is not None
        }
        if carried and unit.upper() not in carried:
            contradicted.add(f"{digits} {unit}")
    for name, operator, digits in _COMPARISON_RE.findall(english):
        # The threshold ends where its number ends, and it starts where its
        # name starts: unbounded, "N > 5" is satisfied by "N > 50" and by
        # "MIN > 5". The right-hand boundary refuses a grouping separator
        # followed by more digits too, since "5.000" and "5 000" are the
        # likelier translation artefact than "50" -- those separators are the
        # ones the number pattern already knows, referenced rather than
        # respelled.
        if not re.search(
            rf"(?<!\w){re.escape(name)}\s*{re.escape(operator)}\s*"
            rf"{re.escape(digits)}(?!\d|{_GROUP_SEPARATOR_RE.pattern}\d)",
            translated,
        ):
            contradicted.add(f"{name} {operator} {digits}")
    for digits in _PERCENT_RE.findall(english):
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(digits)}\s?%", translated):
            contradicted.add(f"{digits}%")
    for digits, suffix in _MAGNITUDE_RE.findall(english):
        # Same every-occurrence rule as the storage arm. "bis zu 5x schneller,
        # etwa 5K Token" is a correct translation whose *first* run of these
        # digits carries an unrelated letter, and reading only that one turned
        # it red -- post-merge that blocks the push for the whole run.
        carried = {
            match.group(1).upper()
            for match in re.finditer(
                rf"(?<![A-Za-z0-9]){re.escape(digits)}([A-Za-z])", translated
            )
        }
        if carried and suffix.upper() not in carried:
            contradicted.add(f"{digits}{suffix}")
    return sorted(contradicted)


def _signed_numbers(text: str) -> list[tuple[str, str]]:
    """(sign, number) for every number this text signs itself.

    A range endpoint is not signed, however its catalog spaced the dash out,
    so any run of separators between a digit and the dash disqualifies it --
    including the non-breaking ones a locale groups numbers with. That test
    is a variable-width look behind, which ``re`` refuses, so the left context
    is read here instead.
    """
    return [
        (match.group(1), match.group(2))
        for match in _SIGNED_NUMBER_RE.finditer(text)
        if not _RANGE_LEFT_CONTEXT_RE.search(text[: match.start()])
    ]


def _invented_signs(english: str, translated: str) -> list[str]:
    """Numbers the translation signed and its English did not.

    A sign is part of the claim and the digits are blind to it: "Range 1-600"
    and "Bereich -1-600" hold the same number multiset, the same two endpoints
    in the same order, and the second states a minimum the setting rejects.

    One direction only, and the corpus is the reason: no shipped English
    string signs a number, so a sign can only arrive with a translation.
    Asking the other way round would be an arm that cannot fire on anything
    the pipeline sends.

    What must not count as a sign is the dash between two range endpoints,
    whatever the catalog put between the left endpoint and the dash: spaces,
    the non-breaking ones these catalogs group numbers with, and the unit or
    percent sign a locale may repeat on both bounds ("1 MB -256 MB",
    "50 % -100 %"). The left context is read in code rather than in a look
    behind, because a look behind here has to be fixed width and none of that
    is.

    The comparison is on canonical numbers, not on their text, so a locale
    regrouping "-1 000" as "-1.000" is the same signed number rather than an
    invented one.
    """
    signed_in_english = {
        (sign, _canonical_number(digits)) for sign, digits in _signed_numbers(english)
    }
    return sorted(
        {
            f"unsigned {digits} written as {sign}{digits}"
            for sign, digits in _signed_numbers(translated)
            if (sign, _canonical_number(digits)) not in signed_in_english
        }
    )


def _reversed_ordered_pairs(english: str, translated: str) -> list[str]:
    """Ordered number pairs the translation states back to front.

    A range is an ordered claim that the value comparison is blind to: both
    endpoints of "Range 1-600" are still present in "Range 600-1", so the
    number multiset balances while the text now names a floor above its
    ceiling.

    Only an actual inversion reports. A catalog is free to write "von 1 bis
    600" instead of setting a dash, and its numbers stay guarded by the value
    comparison either way; demanding the dash form back would fail a correct
    rendering rather than a wrong one.

    A ratio is the same claim with a colon for a dash: "4.5:1" and "1:4,5"
    hold the same two numbers, and the second states a contrast threshold no
    checker would ever set. The separator the English used is carried into
    the report so a ratio is not named as a range.
    """
    translated_bounds = {
        (_canonical_number(low), _canonical_number(high))
        for low, _, high in _ORDERED_PAIR_RE.findall(translated)
    }
    return sorted(
        {
            f"{low}{':' if separator in ':：' else '-'}{high}"
            for low, separator, high in _ORDERED_PAIR_RE.findall(english)
            if (bounds := (_canonical_number(low), _canonical_number(high)))
            and bounds[0] != bounds[1]
            and bounds[::-1] in translated_bounds
            and bounds not in translated_bounds
        }
    )


def _show_numbers(counted: Counter[tuple[str, ...]]) -> list[str]:
    return sorted(".".join(groups) for groups in counted.elements())


def _parity_fault(
    english: str,
    translated: str,
    *,
    tolerated_losses: Counter[tuple[str, ...]] | None = None,
    tolerated_additions: Counter[tuple[str, ...]] | None = None,
    compare_numbers: bool = True,
) -> str:
    """Everything one English/translated pair contradicts, or "" if nothing.

    One function rather than an expression inline in the test, because the
    set of arms it runs is itself a thing that can be broken: dropping any
    one of them from the sum leaves every shipped pair green -- the corpus is
    clean, so an arm that stops being called reports nothing and looks
    identical to an arm that found nothing.
    ``test_the_comparison_runs_every_arm`` holds each one to a case it must
    report, which only works if there is a single place they are summed.

    Numbers are compared as multisets in both directions; a tolerance is
    subtracted by occurrence rather than by value.

    ``compare_numbers=False`` is what the engine asks for, and the reason is
    the tolerance table the merge-time check carries: both of its entries are
    number-count entries. Russian spells "the 5 experimental sub-flags" out in
    words and Chinese translates a clause the English rendering cuts -- each
    correct, each a number the multiset says went missing or appeared. A
    correct translation the engine refuses is retried once and then left for
    the next run to plan again, which is the daily stall this call exists to
    prevent, so the arm that produces those two is left to the check that can
    hold a per-key tolerance. Every fault #2180 repaired by hand -- a dropped
    ``docs/beta.md``, a localised ``enable_tool_search``, "46K" where the
    English says 90% -- is found by the arms that do run here.

    The same switch turns off literal COUNTING, and for the same reason: a
    faithful translation may name a repeated identifier once and pronominalise
    the second mention, which no per-string rule can tell from a corrupted
    duplicate. Whether a name survives at all is still asked of the engine;
    how often it survives is asked only where a tolerance can absorb it.
    """
    losses = tolerated_losses if tolerated_losses is not None else Counter()
    additions = tolerated_additions if tolerated_additions is not None else Counter()
    if compare_numbers:
        lost_numbers = (_numbers(english) - _numbers(translated)) - losses
        gained_numbers = (_numbers(translated) - _numbers(english)) - additions
    else:
        lost_numbers = gained_numbers = Counter()
    lost = (
        _lost_literals(english, translated, count_occurrences=compare_numbers)
        + _lost_magnitudes(english, translated)
        + _invented_signs(english, translated)
        + _reversed_ordered_pairs(english, translated)
        + _localised_hardcoded_name(english, translated)
        + _invented_files(english, translated)
    )
    return ", ".join(
        part
        for part in (
            f"numbers lost {_show_numbers(lost_numbers)}" if lost_numbers else "",
            f"numbers added {_show_numbers(gained_numbers)}" if gained_numbers else "",
            f"identifiers dropped {lost}" if lost else "",
        )
        if part
    )


@cache
def _untranslatable_names() -> frozenset[str]:
    """On-screen names our own Python hardcodes, which must stay English.

    Read from the pipeline at runtime instead of copied, so this check cannot
    drift away from ``translate_locales._untranslatable_name_dropped``, the
    guard that rejects engine output localising one.
    """
    import translate_locales  # type: ignore[import-not-found]

    return frozenset(translate_locales._hardcoded_ui_names())


def _without_sentence_punctuation(literal: str) -> str:
    """A literal with the prose punctuation it swallowed removed.

    A path and a URI both run to the next space, so "see /api/x." and
    "at skill://y)" carry the sentence's period or bracket into the token, and
    requiring it verbatim fails a translation that keeps the literal but ends
    its sentence differently.

    An ellipsis is left alone: ``/api/webhook/...`` is the one live English
    literal ending in punctuation, and there the dots are part of what the
    reader is shown, not the end of a sentence.
    """
    if literal.endswith("..."):
        return literal
    return literal.rstrip(".,;:!?)]}\"'»”")


def _occurrences(literal: str, text: str) -> int:
    """How often the text contains this literal as a whole token.

    Substring, but not *any* substring: the match may not continue into
    another identifier character, so "enable_tool_search_old" no longer counts
    as carrying `enable_tool_search`, nor "configuration.yaml.bak" as carrying
    `configuration.yaml`. A hyphen or a case change is a different matter --
    German writes "/data-Volume" and Swedish "/data-volymen", and both carry
    the original intact. A dotted extension does not count either, so
    "configuration.yaml.bak" is a different file -- and so is
    "other.configuration.yaml", so a dot is excluded on the left as well --
    while a sentence-ending period after the name is not part of the token and
    still matches.

    Counted rather than merely found, because an English string is free to
    name the same identifier twice and four shipped ones do.
    """
    return len(
        re.findall(
            r"(?<![A-Za-z0-9_.])"
            + re.escape(literal)
            + r"(?![A-Za-z0-9_]|\.[A-Za-z0-9])",
            text,
        )
    )


def _carries(literal: str, translated: str) -> bool:
    """Whether the translation contains this literal at all."""
    return _occurrences(literal, translated) > 0


def _lost_literals(
    english: str, translated: str, *, count_occurrences: bool = True
) -> list[str]:
    """English literals absent from the translation, as substrings.

    Substring rather than token equality on purpose: German writes
    "/data-Volume" and Swedish "/data-volymen" and "skill://-resurs", which
    tokenise as different literals while carrying the original intact.
    Re-extracting from the translation reports every one of them as a loss;
    asking whether the English literal is still findable reports none.

    Both arms go through ``_carries``, so an argument is held to the same
    boundary rule as an identifier: with plain containment, "scope='snapshots'"
    satisfies "scope='snapshot'" while naming a different value.

    Hardcoded on-screen names are left to ``_localised_hardcoded_name``, which
    applies the pipeline's own rule to them. Excluding them here is structural
    rather than load-bearing: every name it lists is several words long and
    none tokenises as a literal, so the filter removes nothing today. It is
    what keeps a name that later gains an extractable shape from reporting
    twice under two different descriptions.

    ``count_occurrences`` compares how OFTEN the name survives, the same way
    the number arm compares multisets. Four shipped English strings name an
    identifier twice -- ``ha_get_skill_guide`` in the strict-best-practices
    help, ``skill_content`` in the one beside it, ``ChatGPT`` in two notices --
    and asking only whether the name survives *somewhere* accepts a translation
    that corrupts one of the two. Both sides are counted with the same
    instrument; measured across the 6751 shipped pairs, counting reports
    nothing that presence did not.

    It is off for the engine, and for the same reason the number multisets are:
    a faithful translation may state a repeated name once and pronominalise the
    second mention, and there is nowhere to record a per-key tolerance at
    acceptance time. Refusing that costs a retry and leaves the key to be
    planned again tomorrow, which is the daily stall the engine-side call
    exists to prevent. The merge-time check, which can carry a tolerance and is
    read by a human when it fails, keeps the count.
    """
    protected = _untranslatable_names()
    candidates = {
        stripped
        for literal in _LITERAL_RE.findall(english)
        if (stripped := _without_sentence_punctuation(literal))
        and stripped not in protected
        and stripped not in _PROSE_ABBREVIATIONS
    } | set(_QUOTED_ASSIGNMENT_RE.findall(english))
    lost = []
    for literal in candidates:
        # The extraction and the boundary rule can disagree -- ".env" is
        # extracted out of ".env.local", which the boundary rule then refuses
        # to find in its own English. A zero there would make every comparison
        # against it vacuously true and the literal unreportable for good, so
        # it falls back to demanding the name once, which is what the presence
        # test demanded before counting existed.
        expected = (_occurrences(literal, english) if count_occurrences else 1) or 1
        kept = _occurrences(literal, translated)
        if kept >= expected:
            continue
        # A name that survives in part is not a name that vanished, and the
        # maintainer reading the failure would otherwise grep the catalog,
        # find the literal, and have nothing to go on.
        lost.append(literal if kept == 0 else f"{literal} (kept {kept} of {expected})")
    return sorted(lost)


def _invented_files(english: str, translated: str) -> list[str]:
    """Config files the translation names and its English does not.

    Literal parity is otherwise one-directional: it asks what the English has
    that the translation lost, so a translation naming a file of its own
    passes. That is how three catalogs kept describing ``packages/*.yaml`` and
    ``themes/*.yaml`` for a tool whose English stopped naming either -- the
    English literal they *did* carry was still there, and nothing looked at
    the rest.

    Only files, deliberately, and the measurement is why. Asking the same
    question of every literal shape reported 62 rows on the catalogs as they
    stood before the repair below: six real ones and 56 correct translations.
    German, Dutch and Swedish compound with a slash ("Schreib-/Schreib-Tools"),
    which the path arm reads as a literal the English never had, and Swedish
    abbreviates "till exempel" as "t.ex" -- with the six repaired those 56 are
    all that shape still reports. A file name has no such prose form: a catalog
    that names one is naming a file on the reader's disk, and restricted to
    that, the same question reported exactly the six, across three keys.

    A dropped duplicate is the other half of the number arm's multiset rule
    and is not implemented here: no shipped English string names the same file
    twice, so the arm would be structurally empty rather than verified, and
    the reverse direction is what the live corpus actually needed.
    """
    return sorted(
        {
            stripped
            for literal in _FILE_RE.findall(translated)
            if (stripped := _without_sentence_punctuation(literal))
            and not _carries(stripped, english)
        }
    )


def _localised_hardcoded_name(english: str, translated: str) -> list[str]:
    """The on-screen name this translation localised away, if any.

    The pipeline already refuses engine output that translates one of these,
    but that gate only ever runs while *accepting* new output. A catalog edited
    by hand, or one pinned before the name existed, is never re-read by it:
    the baseline still matches, so the sync plans no rewrite, and a reader is
    left hunting for an entry title the integration does not display. Asking
    the same question of the pinned pairs costs one call and needs no second
    rule -- ``translate_locales`` owns it, and it is called rather than copied
    so the two cannot drift apart.
    """
    import translate_locales

    dropped = translate_locales._untranslatable_name_dropped(english, translated)
    return [dropped] if dropped is not None else []
