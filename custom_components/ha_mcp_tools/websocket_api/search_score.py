"""Text tiering and scoring helpers for ``ha_mcp_tools/search``."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from .constants import _SPLIT_RE, FUZZY_THRESHOLD, HIDDEN_SCORE_PENALTY


# =============================================================================
# Scoring — mirrors the server's tiers (guarded by the golden parity test)
# =============================================================================
def _apply_hidden_penalty(score: int, hidden: bool) -> int:
    """Reduce ``score`` by :data:`HIDDEN_SCORE_PENALTY` for hidden entities.

    Mirrors ``utils.fuzzy_search.apply_hidden_penalty`` so the two rankings
    stay consistent.
    """
    s = int(score)
    return max(0, s - HIDDEN_SCORE_PENALTY) if hidden else s


def _calc_ratio(a: str, b: str) -> int:
    """SequenceMatcher ratio (0-100). Mirrors ``fuzzy_search.calculate_ratio``."""
    return int(SequenceMatcher(None, a, b, autojunk=False).ratio() * 100)


def _tokenize(text: str) -> list[str]:
    """Split on ``.``/``_``/``-``/whitespace, lowercase, drop empties.

    Mirrors ``utils.fuzzy_search.tokenize``.
    """
    return [t for t in _SPLIT_RE.split(text.lower()) if t]


def _sep_normalized(text: str) -> str:
    """Collapse ``.``/``_``/``-``/whitespace runs to single spaces.

    The server's fuzzy engine (BM25) tokenizes query and documents with the
    same splitter, making ``input_boolean`` and ``input boolean`` equivalent
    queries (pinned by the e2e underscore/space-equivalence test). Comparing
    separator-normalized strings replicates that equivalence for the
    component's tier scorer.
    """
    return " ".join(_tokenize(text))


def _text_tier(query_lower: str, texts: Any, *, fuzzy: bool) -> int | None:
    """Entity tier: 100 (exact), 80 (substring), fuzzy ratio (>=threshold), or None.

    Mirrors the server's ``_match_exact_search_entity`` (100/80) over the entity
    id + friendly name, extended to the joined alias/area/floor/label/domain/
    device texts. In fuzzy mode comparisons run on BOTH the raw strings and
    their separator-normalized forms (unified tokenization — ``_``/space
    equivalence), with a whole-string ``calculate_ratio`` fallback surfacing
    typos above :data:`FUZZY_THRESHOLD`. Exact mode stays raw-only for
    byte-parity with the server's exact path.
    """
    query_norm = _sep_normalized(query_lower) if fuzzy else ""
    best_substring: int | None = None
    best_ratio = 0
    for text in texts:
        if not text:
            continue
        tier, ratio = _tier_one_text(query_lower, query_norm, str(text).lower(), fuzzy)
        if tier == 100:
            return 100
        if tier == 80:
            best_substring = 80
        elif ratio > best_ratio:
            best_ratio = ratio
    if best_substring is not None:
        return best_substring
    if fuzzy and best_ratio >= FUZZY_THRESHOLD:
        return best_ratio
    return None


def _tier_one_text(
    query_lower: str, query_norm: str, text_lower: str, fuzzy: bool
) -> tuple[int | None, int]:
    """Score one candidate text: ``(tier, ratio)``.

    Tier 100 = exact (raw, or separator-normalized in fuzzy mode); tier 80 =
    substring (same two forms); otherwise ``ratio`` carries the fuzzy
    whole-string fallback (0 when not in fuzzy mode).
    """
    if query_lower == text_lower:
        return 100, 0
    text_norm = _sep_normalized(text_lower) if fuzzy and query_norm else ""
    if text_norm and query_norm == text_norm:
        return 100, 0
    if query_lower in text_lower:
        return 80, 0
    if text_norm and query_norm in text_norm:
        return 80, 0
    if fuzzy:
        return None, _calc_ratio(query_lower, text_lower)
    return None, 0


def _name_tier(query_lower: str, texts: Any, *, exact: bool) -> int | None:
    """Config-name tier: substring => 100 (not 80), else fuzzy ratio or None.

    Config name matches are binary 100/0 in the server's exact path
    (``_score_deep_match``: ``name_exact = 100 if query in id/name else 0``),
    unlike entity matches which have the 80 substring tier.
    """
    query_norm = "" if exact else _sep_normalized(query_lower)
    best_ratio = 0
    for text in texts:
        if not text:
            continue
        text_lower = str(text).lower()
        if query_lower in text_lower:
            return 100
        if not exact:
            if query_norm and query_norm in _sep_normalized(text_lower):
                return 100
            ratio = _calc_ratio(query_lower, text_lower)
            best_ratio = max(best_ratio, ratio)
    if not exact and best_ratio >= FUZZY_THRESHOLD:
        return best_ratio
    return None


def _config_score(
    query_lower: str,
    entity_id: str,
    name: str,
    config_dict: dict[str, Any] | None,
    *,
    exact: bool,
    secret_values: frozenset[str] = frozenset(),
) -> tuple[int, bool, bool] | None:
    """Score a config surface: (total, match_in_name, match_in_config) or None.

    Exact mode is binary 100/0 with a threshold of 100 (server parity); fuzzy
    mode floors at :data:`FUZZY_THRESHOLD`. ``secret_values`` scrubs the body
    match corpus (see :func:`_search_in_dict_exact`).
    """
    name_score = _name_tier(query_lower, [entity_id, name], exact=exact) or 0
    config_score = _config_body_score(
        query_lower, config_dict, exact=exact, secret_values=secret_values
    )
    threshold = 100 if exact else FUZZY_THRESHOLD
    total = max(name_score, config_score)
    if total < threshold:
        return None
    return total, name_score >= threshold, config_score >= threshold


def _config_body_score(
    query_lower: str,
    config_dict: dict[str, Any] | None,
    *,
    exact: bool,
    secret_values: frozenset[str] = frozenset(),
) -> int:
    """Match the query against a config body's keys/values.

    Exact => 100/0 substring (``_search_in_dict_exact`` parity). Fuzzy adds a
    token-vs-token ``calculate_ratio`` fallback (the server's tier-3 path).
    ``secret_values`` scrubs resolved-``!secret`` leaves from the corpus.
    """
    if config_dict is None:
        return 0
    if _search_in_dict_exact(config_dict, query_lower, secret_values) >= 100:
        return 100
    if exact:
        return 0
    leaves: list[str] = []
    _collect_string_leaves(config_dict, leaves, secret_values)
    query_tokens = _tokenize(query_lower)
    if not query_tokens:
        return 0
    doc_tokens = {tok for leaf in leaves for tok in _tokenize(leaf)}
    best = 0
    for qt in query_tokens:
        for dt in doc_tokens:
            best = max(best, _calc_ratio(qt, dt))
    return best if best >= FUZZY_THRESHOLD else 0


def _search_in_dict_exact(
    data: Any, query_lower: str, secret_values: frozenset[str] = frozenset()
) -> int:
    """Exact substring search in nested structures (100 or 0).

    Mirrors ``smart_search._scoring.ScoringMixin._search_in_dict_exact``, plus a
    secret scrub: a string leaf that exactly equals a known secret value never
    contributes a match (see :func:`_load_secret_values`), so a query equal to a
    resolved ``!secret`` cannot be confirmed via ``match_in_config``. Keys and
    non-string scalars are never secrets, so they are matched as before.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if query_lower in str(key).lower():
                return 100
            if _search_in_dict_exact(value, query_lower, secret_values) >= 100:
                return 100
        return 0
    if isinstance(data, (list, tuple)):
        for item in data:
            if _search_in_dict_exact(item, query_lower, secret_values) >= 100:
                return 100
        return 0
    return _leaf_exact_score(data, query_lower, secret_values)


def _leaf_exact_score(
    data: Any, query_lower: str, secret_values: frozenset[str]
) -> int:
    """Exact substring score for a scalar leaf (100 or 0).

    A string leaf that exactly equals a known secret value scores 0 — the scrub
    that keeps a resolved ``!secret`` out of the match corpus.
    """
    if isinstance(data, str):
        if data in secret_values:
            return 0
        return 100 if query_lower in data.lower() else 0
    if data is not None:
        return 100 if query_lower in str(data).lower() else 0
    return 0


def _collect_string_leaves(
    data: Any, out: list[str], secret_values: frozenset[str] = frozenset()
) -> None:
    """Recursively collect string representations. Mirrors the server helper.

    A string leaf that exactly equals a known secret value is dropped so it
    never reaches the fuzzy token corpus (the scrub in :func:`_search_in_dict_exact`
    covers the exact path).
    """
    if isinstance(data, dict):
        for key, value in data.items():
            out.append(str(key))
            _collect_string_leaves(value, out, secret_values)
    elif isinstance(data, (list, tuple)):
        for item in data:
            _collect_string_leaves(item, out, secret_values)
    elif isinstance(data, str):
        if data not in secret_values:
            out.append(data)
    elif data is not None:
        out.append(str(data))
