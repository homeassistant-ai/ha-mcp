"""Relevance scoring of config dicts (BM25 fuzzy + exact substring)."""

import logging
from typing import Any

from ...utils.fuzzy_search import BM25Scorer, calculate_ratio, tokenize
from ._base import _SearchBase

logger = logging.getLogger(__name__)


class ScoringMixin(_SearchBase):
    """Query-vs-config relevance scoring shared by the deep-search family."""

    def _score_deep_match(
        self,
        entity_id: str,
        friendly_name: str,
        fuzzy_name_score: int,
        config_match_score: int,
        query_lower: str,
        exact_match: bool,
    ) -> tuple[int, int, bool]:
        """Compute total score, threshold, and match_in_name for a deep search result.

        Returns (total_score, threshold, match_in_name).
        """
        if exact_match:
            name_exact = (
                100
                if query_lower in entity_id.lower()
                or query_lower in friendly_name.lower()
                else 0
            )
            total_score = max(name_exact, config_match_score)
            return total_score, 100, name_exact >= 100
        else:
            total_score = max(fuzzy_name_score, config_match_score)
            threshold = self.settings.fuzzy_threshold
            return total_score, threshold, fuzzy_name_score >= threshold

    def _search_in_dict(
        self,
        data: dict[str, Any] | list[Any] | Any,
        query: str,
        exact_match: bool = False,
    ) -> int:
        """Search for query in nested dictionary/list structures.

        When exact_match is True, uses substring matching (returns 100 if found, 0 if not).
        When exact_match is False, collects all string leaves, tokenizes them into a
        single BM25 document, and scores against the query tokens.  Falls back to
        token-level SequenceMatcher if BM25 returns 0 (typo correction).
        """
        if exact_match:
            return self._search_in_dict_exact(data, query)

        # Fuzzy path: collect all string leaves, build a single tokenised document
        leaves: list[str] = []
        self._collect_string_leaves(data, leaves)
        if not leaves:
            return 0

        query_tokens = tokenize(query)
        if not query_tokens:
            return 0

        # Build a single flat token list from all leaves
        doc_tokens: list[str] = []
        for leaf in leaves:
            doc_tokens.extend(tokenize(leaf))

        if not doc_tokens:
            return 0

        # Use BM25 with a 1-document corpus (the config dict as a single doc)
        scorer = BM25Scorer()
        scorer.fit([doc_tokens])
        raw = scorer.score(query_tokens, 0)

        if raw > 0:
            # Normalise against the theoretical max (sum of IDF per query
            # token). With a 1-document corpus every token's IDF is identical
            # (~0.288 with smoothing), so the ratio effectively measures how
            # many query tokens the config contains. Cap at 100 for the edge
            # case where high TF pushes raw above the sum-of-IDFs baseline.
            max_possible = scorer.max_possible_score(query_tokens)
            if max_possible > 0:
                return min(100, round(raw / max_possible * 100))
            logger.warning(
                "BM25 scored > 0 but max_possible IDF is 0; "
                "query_tokens=%s, doc_tokens_len=%d",
                query_tokens,
                len(doc_tokens),
            )
            return 100

        # Tier-3 fallback: token-level SequenceMatcher for typos
        logger.debug(
            "BM25 returned 0 for query_tokens=%s; "
            "falling back to SequenceMatcher typo scoring over %d unique tokens",
            query_tokens,
            len(set(doc_tokens)),
        )
        best = 0
        for qt in query_tokens:
            for dt in set(doc_tokens):
                best = max(best, calculate_ratio(qt, dt))
        return best if best >= 70 else 0

    @staticmethod
    def _collect_string_leaves(
        data: dict[str, Any] | list[Any] | Any, out: list[str]
    ) -> None:
        """Recursively collect all string representations from nested data."""
        if isinstance(data, dict):
            for key, value in data.items():
                out.append(str(key))
                ScoringMixin._collect_string_leaves(value, out)
        elif isinstance(data, list):
            for item in data:
                ScoringMixin._collect_string_leaves(item, out)
        elif isinstance(data, str):
            out.append(data)
        elif data is not None:
            out.append(str(data))

    @classmethod
    def _search_in_dict_exact(
        cls,
        data: dict[str, Any] | list[Any] | Any,
        query: str,
    ) -> int:
        """Exact substring search in nested structures (returns 100 or 0)."""
        if isinstance(data, dict):
            return cls._exact_in_dict(data, query)
        if isinstance(data, list):
            return cls._exact_in_list(data, query)
        if isinstance(data, str):
            return 100 if query in data.lower() else 0
        if data is not None:
            return 100 if query in str(data).lower() else 0
        return 0

    @classmethod
    def _exact_in_dict(cls, data: dict[str, Any], query: str) -> int:
        """Exact-match scan over a dict's keys and recursively over its values."""
        for key, value in data.items():
            if query in str(key).lower():
                return 100
            if cls._search_in_dict_exact(value, query) >= 100:
                return 100
        return 0

    @classmethod
    def _exact_in_list(cls, data: list[Any], query: str) -> int:
        """Exact-match scan recursively over a list's items."""
        for item in data:
            if cls._search_in_dict_exact(item, query) >= 100:
                return 100
        return 0
