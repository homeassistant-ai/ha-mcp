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

The two calls differ in three deliberate respects, all selected by one
``gate`` argument and spelled out on ``_parity_fault``: the engine does not
compare number multisets, does not count literal occurrences, and does not
report a dropped unit.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any, Literal

# Both calls into the pipeline below name a sibling script, and importing this
# module does not put `scripts/` on the path -- `translate_locales` does that
# for its own siblings. A caller that imported the rules by file path got a
# clean import and a ModuleNotFoundError at the first comparison, so the
# directory is added here. Appended rather than prepended: what is missing is
# never on the path already, and prepending would let a future
# `scripts/<stdlib-name>.py` shadow the standard library process-wide.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(_SCRIPTS_DIR))


@cache
def _pipeline() -> Any:
    """The translation engine's own module, whose rules these two arms reuse.

    Loaded from the sibling path rather than by name. A plain import resolves
    against the whole search path, so a ``translate_locales.py`` in the
    caller's working directory or anywhere earlier on it wins -- and the two
    arms below would then be asking a stranger what the untranslatable names
    are.

    A module already registered under that name is reused only when it IS the
    sibling: the engine imports this one, and re-executing it would give the
    process two copies with separate state. Reusing the entry unchecked put
    the hole back one level up -- a stranger loaded first still answered --
    so the origin is compared before the entry is trusted. A stranger keeps
    its slot; the sibling is loaded under a private name beside it, because
    evicting a module another importer is holding is not this module's call.
    """
    sibling = _SCRIPTS_DIR / "translate_locales.py"
    registered = sys.modules.get("translate_locales")
    if (
        registered is not None
        and Path(getattr(registered, "__file__", "") or "").resolve()
        == sibling.resolve()
    ):
        return registered
    name = "translate_locales" if registered is None else "_locale_rules_pipeline"
    if (loaded := sys.modules.get(name)) is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, sibling)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable
        raise ImportError(f"cannot load translate_locales from {_SCRIPTS_DIR}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the engine's own module-level imports run
    # during exec_module, and one of them can reach back here.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# A digit run preceded by a letter or another digit belongs to an identifier
# ("Z2M", "MQTT5"), not to a claim about quantity, so it is not a number a
# translation owes. A trailing unit is deliberately left out of the token:
# a locale that writes "5 тыс." for "5K" still matches on the digits, while
# "46K" where the English says 90% and 5K still reports as changed.
#
# Separators inside a number are kept as GROUP BOUNDARIES rather than deleted.
# Deleting them tolerates locale punctuation but also erases the difference
# between the "4.5:1" contrast ratio and "45:1", and between the minimum
# version floor "1.2.4" and "12.4" -- both live English strings. That 1.2.4
# is a number a help string states, not MIN_COMPONENT_VERSION, which is a
# different value and not pinned here. Comparing the tuple of groups keeps
# "4.5" and "4,5" equal while "45" stays a different number.
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,   ]\d+)*")
# A magnitude suffix is part of the claim: "5K" and "5M" share their digits.
_MAGNITUDE_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)([KMGT])(?![A-Za-z])")
# A percentage is a unit every catalog keeps, spaced or not: four write
# "90 %" and four "90%", so the sign is required but the space is free. The
# fullwidth sign counts as the sign: a CJK catalog may set U+FF05, and
# demanding the ASCII one would fail a correct rendering.
_PERCENT_RE = re.compile(r"(?<![A-Za-z0-9])(\d+)\s?[%％]")
# A storage unit is part of the claim too: "1-256 MB" and "1-256 GB" differ by
# three orders of magnitude while the digits match.
_COMPOUND_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d+)\s*([A-Za-zА-Яа-я]{2})(?![A-Za-zА-Яа-я])"
)
_KNOWN_UNITS = frozenset({"KB", "MB", "GB", "TB"})
# The same four units as the catalogs may spell them. Russian transliterates
# the prefix and abbreviates "байт"; French sets "o" for "octet". Two of the
# eight are live -- the shipped catalogs write "МБ" once and "Mo" once, beside
# eight ASCII "MB" -- and the rest are those two vocabularies at the other
# magnitudes, listed so a catalog reaching for one is compared rather than
# waved through. A token outside the table stays uncomparable, which is the
# safe arm.
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


# A localised spelling is matched regardless of case -- "Гб" and "go" are
# ordinary renderings of table entries -- with one exception the corpus
# forces: "to" is an English word, and `custom_components/ha_mcp_tools/
# translations/en.json` ships "9584 to", which case-folding would read as
# terabytes. The Cyrillic entries have no such collision.
_CASE_COLLIDING_UNITS = frozenset({"to"})
_LOCALISED_UNITS_FOLDED = {
    spelling.casefold(): canonical
    for spelling, canonical in _LOCALISED_UNITS.items()
    if spelling.casefold() not in _CASE_COLLIDING_UNITS
}


def _canonical_unit(unit: str) -> str | None:
    """One storage unit as its English spelling, or None if not comparable."""
    if unit.upper() in _KNOWN_UNITS:
        return unit.upper()
    if unit in _LOCALISED_UNITS:
        return _LOCALISED_UNITS[unit]
    return _LOCALISED_UNITS_FOLDED.get(unit.casefold())


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
# The stem is ASCII on purpose, and non-empty. `\w` matches ideographs and
# Cyrillic, so a string that sets no space around the name hands the reverse
# arm "在configuration.yaml" -- a file nobody wrote, built around one the
# English does mention -- and an empty stem makes a bare "（.yaml 文件）" a
# file name. No shipped catalog writes either shape today (measured: zero
# adjacent CJK-or-Cyrillic-to-Latin runs, zero bare extensions), so this is
# hardening for what the engine can propose, not a repair of the corpus.
# The cost is one direction of coverage: a filename with a non-ASCII stem,
# "конфигурация.yaml", is no longer seen as invented either.
_FILE_PATTERN = r"[A-Za-z0-9_.~*/-]+\.(?:yaml|yml|json|py|md|txt)"
_FILE_RE = re.compile(_FILE_PATTERN)

_LITERAL_RE = re.compile(
    rf"""(?:
        {_FILE_PATTERN}                         # files: configuration.yaml
      | [a-z][a-z0-9]*(?:_[a-z0-9]+)+           # snake_case: enable_tool_search
      | [A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+           # ALL_CAPS: DISABLED_TOOLS
      | (?<![\w/<])~?/[\w.*-]+(?:/[\w.*-]+)*    # paths: /api/settings/features
        # A hidden name has no extension and no leading separator, so neither
        # the file arm nor the path arm sees it, and the dotted-identifier arm
        # below is blocked by the leading dot. The live case is ".storage",
        # which the component tells readers the deny floor blocks: without
        # this alternative a translation dropping that name reports nothing,
        # from any arm. It sits after the file and path arms so a name inside
        # a path ("~/.ha-mcp/x") or in front of an extension stays whole, and
        # it takes its own dotted continuation, so ".env" is not split out of
        # ".env.local" and then reported against a translation that kept it.
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
    "4.5:1" ratio and the version floor "1.2.4" a help string states stay
    distinct from "45" and "12.4".
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


def _attaches_a_word(digits: str, translated: str) -> bool:
    """Whether the translation writes a word onto these digits.

    The discriminator between a unit spelled out and a unit dropped. Only
    horizontal whitespace may separate the two -- a newline or any punctuation
    means the word belongs to the next sentence, not to this number -- and the
    word is any letter run, in any script, because the spellings that have to
    pass here are "гигабайт", "megabajtów" and "tys." rather than a list this
    module could keep current.
    """
    return (
        re.search(rf"(?<![A-Za-z0-9]){re.escape(digits)}[^\S\n]*[^\W\d_]", translated)
        is not None
    )


def _storage_units_carried(digits: str, translated: str) -> set[str]:
    """The storage units this translation attaches to these digits.

    EVERY occurrence of the digits, not the first one: "jusqu'a 256 Go puis
    256 MB" carries the unit on its second, and settling on the first reported
    a correct translation as a contradiction while missing this arm's own case.
    One occurrence spelling the unit right is the translation carrying the
    claim. A spelling outside the table stays uncomparable and is left out,
    which is the safe arm.
    """
    return {
        canonical
        for match in re.finditer(
            rf"(?<![A-Za-z0-9]){re.escape(digits)}\s*([A-Za-zА-Яа-я]{{2}})"
            r"(?![A-Za-zА-Яа-я])",
            translated,
        )
        if (canonical := _canonical_unit(match.group(1))) is not None
    }


def _magnitude_suffixes_carried(digits: str, translated: str) -> set[str]:
    """The magnitude suffixes this translation writes straight onto these digits.

    Same every-occurrence rule as the storage arm: "bis zu 5x schneller, etwa
    5K Token" is a correct translation whose *first* run of these digits
    carries an unrelated letter, and reading only that one turned it red --
    post-merge that blocks the push for the whole run.

    The whole attached letter run is the suffix, not its first letter. "5KB"
    is five kilobytes where "5K" is five thousand, and reading only the "K"
    accepted the one for the other; a run that is not the expected suffix is
    a different claim, so it reports. Truncating the run instead of comparing
    it is worse than the gap it closes -- the arm then also stops seeing "5M"
    stated as "5KB", which it catches today.
    """
    return {
        match.group(1).upper()
        for match in re.finditer(
            rf"(?<![A-Za-z0-9]){re.escape(digits)}([A-Za-z]+)", translated
        )
    }


def _unit_fault(
    digits: str,
    expected: str,
    carried: set[str],
    translated: str,
    *,
    require_unit: bool,
    claim: str,
) -> str | None:
    """What one digits-and-unit claim contradicts, or None.

    The two unit arms ask the same three-way question and differ only in what
    counts as a unit, so the decision lives here once: a translation carrying
    some unit for these digits must carry the right one, and a translation
    carrying none has dropped it -- unless it attached a word instead, which is
    a unit spelled out, or the caller is the engine, which tolerates the drop.
    """
    if carried:
        return None if expected in carried else claim
    if require_unit and not _attaches_a_word(digits, translated):
        return f"{claim} dropped"
    return None


def _lost_magnitudes(
    english: str, translated: str, *, require_unit: bool = True
) -> list[str]:
    """Magnitude suffixes the translation contradicts, and at the merge gate
    the ones it drops.

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

    A translation that attaches nothing at all to the digits -- "Grenze ist
    1-256" for "limit is 1-256 MB" -- states a bound without its unit, and
    ``require_unit`` decides whether that reports. It is the same asymmetry
    ``gate`` encodes for the numbers: at the merge gate a human reads the
    failure and the key can carry a tolerance, so the drop is worth naming; on
    the engine path a refusal holds a whole partial run back, and the cheapest
    correct rendering of a unit is not always an abbreviation.
    A spelled-out unit is not a dropped one and is silent under either mode:
    "предел 1-256 гигабайт" attaches a word to the digits, which is what
    separates it from the bare bound. Only horizontal space may sit between,
    so "256. Die Grenze" is still a drop -- that word starts a sentence rather
    than naming a unit. A word that is neither ("1-256 pro Datei") passes, and
    that is the tolerance this rule keeps: it reports the shape nothing else
    can carry, not every wrong one.
    """
    contradicted = set()
    for digits, unit in _COMPOUND_UNIT_RE.findall(english):
        if unit.upper() not in _KNOWN_UNITS:
            continue
        fault = _unit_fault(
            digits,
            unit.upper(),
            _storage_units_carried(digits, translated),
            translated,
            require_unit=require_unit,
            claim=f"{digits} {unit}",
        )
        if fault:
            contradicted.add(fault)
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
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(digits)}\s?[%％]", translated):
            contradicted.add(f"{digits}%")
    for digits, suffix in _MAGNITUDE_RE.findall(english):
        fault = _unit_fault(
            digits,
            suffix.upper(),
            _magnitude_suffixes_carried(digits, translated),
            translated,
            require_unit=require_unit,
            claim=f"{digits}{suffix}",
        )
        if fault:
            contradicted.add(fault)
    return sorted(contradicted)


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

    Zero yield is not zero exposure, which is why this arm outlived a pass
    that cut the arms nothing had fired on: 22 shipped English strings carry
    an ordered pair -- every timeout, retry and size bound in the advanced
    settings, plus the contrast ratio -- and each one is a bound a reader
    acts on. Nothing else can see an inversion: the digits are identical in
    both directions, so neither multiset, magnitudes nor literals report it.

    Equal endpoints need no exclusion of their own, and an explicit one stood
    here until a mutation showed it could not change a result: "5-5" reversed
    is itself, so "the reversal is present" and "the original is not" cannot
    both hold for it.
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
            # A pair the translation also states the right way round is not
            # inverted -- "1-600 (nicht 600-1)" carries both, and demanding
            # the wrong one be absent would report a rendering that spells the
            # mistake out to warn against it.
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
    gate: Literal["merge", "engine"] = "merge",
) -> str:
    """Everything one English/translated pair contradicts, or "" if nothing.

    One function rather than an expression inline in the test, because the
    set of arms it runs is itself a thing that can be broken: dropping any
    one of them from the sum leaves every shipped pair green -- the corpus is
    clean, so an arm that stops being called reports nothing and looks
    identical to an arm that found nothing.
    ``test_the_comparison_runs_every_arm`` holds each one to a case it must
    report, which only works if there is a single place they are summed.

    ``gate`` names which of the two callers is asking, because three of the
    arms can be asked a stricter question by one of them than by the other.
    One dial rather than three: every one of the three splits on the same
    property -- whether a human reads the failure and the key can carry a
    tolerance (merge) or a refusal holds a whole partial run back (engine) --
    so three independent switches would only make it possible to set them
    inconsistently.

    ``"merge"`` compares number multisets in both directions and subtracts a
    tolerance by occurrence, counts how often each literal survives, and
    reports a unit the translation dropped.

    ``"engine"`` narrows all three. Numbers report only when one was swapped
    for another -- when the pair loses a number AND gains one -- and the
    corpus is the reason: across the 6751 shipped pairs, every number
    difference is one-sided (Russian spells "the 5 experimental sub-flags"
    out in words; Chinese keeps a clause the English rendering cuts) and NONE
    is a swap. Asking the full multiset at acceptance time would refuse those
    two correct strings on every run, retry once, and leave their keys to be
    planned again tomorrow -- the daily stall this call exists to prevent.
    Asking nothing let a swapped number through to land as a backfilled key,
    where the merge gate reports it and holds the whole tree back instead. The
    swap is the shape that is wrong in every language, so it is the shape the
    engine refuses.

    Literal counting narrows for the same reason: a faithful translation may
    name a repeated identifier once and pronominalise the second mention, and
    at acceptance time there is nowhere to record that per key. Four shipped
    English strings name an identifier twice, so the merge side keeps the
    count -- with only presence, a translation that corrupts one of the two
    occurrences passes both gates.

    The dropped-unit arm narrows on the same grounds; ``_lost_magnitudes``
    spells out what separates a dropped unit from a spelled-out one.
    """
    losses = tolerated_losses if tolerated_losses is not None else Counter()
    additions = tolerated_additions if tolerated_additions is not None else Counter()
    lost_numbers = (_numbers(english) - _numbers(translated)) - losses
    gained_numbers = (_numbers(translated) - _numbers(english)) - additions
    merge_gate = gate == "merge"
    if not merge_gate and not (lost_numbers and gained_numbers):
        lost_numbers = gained_numbers = Counter()
    lost = (
        _lost_literals(english, translated, count_occurrences=merge_gate)
        + _lost_magnitudes(english, translated, require_unit=merge_gate)
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
    return frozenset(_pipeline()._hardcoded_ui_names())


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

    Both arms go through ``_occurrences``, so an argument is held to the same
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
    nothing that presence did not, so it is exposure it covers rather than a
    fault it found.

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
    dropped = _pipeline()._untranslatable_name_dropped(english, translated)
    return [dropped] if dropped is not None else []
