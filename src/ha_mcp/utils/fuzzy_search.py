"""
Fuzzy entity search utilities for Home Assistant MCP server.

This module provides two search strategies:
- BM25 keyword search (primary fuzzy path): tokenized scoring with IDF term weighting,
  effective for multi-word queries and short entity-name corpora.
- SequenceMatcher (tier-3 fallback): character-level similarity for single-token typo
  correction when BM25 returns nothing.

See issue #851 for background on the BM25 migration.
"""

import logging
import math
import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tokenizer for HA entity IDs and friendly names
# ---------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"[._\-\s]+")

# Score subtraction for entities marked ``hidden_by`` in the entity
# registry. Hidden entities still surface in results, but a 20-point
# penalty pushes them below comparable visible matches when both are
# emitted by the same search branch. Picked so that an exact id/name
# hit on a hidden entity (raw 100 → 80) still ranks above a fuzzy
# threshold-floor visible match (60-70), but loses to any visible
# entity scoring 80+.
HIDDEN_SCORE_PENALTY = 20


def apply_hidden_penalty(score: int, hidden_by: Any) -> int:
    """Return ``score`` reduced by :data:`HIDDEN_SCORE_PENALTY` when
    ``hidden_by`` indicates a hidden entity. Used by every search
    branch that emits a ``score`` field so the ranking is consistent.
    Coerces ``score`` to ``int`` so a stray float caller can't break
    the result-dict's int contract for ``score``.
    """
    s = int(score)
    if hidden_by is not None:
        return max(0, s - HIDDEN_SCORE_PENALTY)
    return s


def tokenize(text: str) -> list[str]:
    """Split text on `.`, `_`, `-`, and whitespace, lowercase, drop empties."""
    return [t for t in _SPLIT_RE.split(text.lower()) if t]


def _strip_separators(text: str) -> str:
    """Strip ``.``, ``_``, ``-``, whitespace from *text* and lowercase.

    Used to add elided-separator forms to the BM25 corpus so queries
    like ``bedlight`` match tokens like ``bed_light``.
    """
    return _SPLIT_RE.sub("", text.lower())


# ---------------------------------------------------------------------------
# BM25 scorer – lightweight, zero-dependency
# ---------------------------------------------------------------------------


class BM25Scorer:
    """BM25 (Okapi) scorer tuned for short HA entity-name documents.

    Parameters are set conservatively for corpora of 2-5 token documents:
      k1=1.2  - moderate term-frequency saturation
      b=0.5   - reduced length-normalization (entity names are uniformly short)
    """

    def __init__(self, k1: float = 1.2, b: float = 0.5) -> None:
        self.k1 = k1
        self.b = b
        # Populated by fit()
        self._idf: dict[str, float] = {}
        self._doc_tokens: list[list[str]] = []
        self._doc_lens: list[int] = []
        self._avgdl: float = 0.0

    # -- corpus building ----------------------------------------------------

    def fit(self, corpus: list[list[str]]) -> None:
        """Build IDF table from a pre-tokenized corpus."""
        self._doc_tokens = corpus
        n = len(corpus)
        if n == 0:
            return

        self._doc_lens = [len(doc) for doc in corpus]
        self._avgdl = sum(self._doc_lens) / n
        # Guard against all-empty corpora: avoids nan from 0/0 in length normalization
        if self._avgdl == 0.0:
            self._avgdl = 1.0

        # document frequency per token
        df: dict[str, int] = {}
        for doc in corpus:
            seen: set[str] = set()
            for token in doc:
                if token not in seen:
                    df[token] = df.get(token, 0) + 1
                    seen.add(token)

        # IDF with smoothing (Robertson variant)
        self._idf = {
            token: math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
            for token, freq in df.items()
        }

    # -- scoring ------------------------------------------------------------

    def score(self, query_tokens: list[str], doc_index: int) -> float:
        """Return the BM25 score for *query_tokens* against document at *doc_index*."""
        doc = self._doc_tokens[doc_index]
        dl = self._doc_lens[doc_index]

        # term frequency in this document
        tf: dict[str, int] = {}
        for t in doc:
            tf[t] = tf.get(t, 0) + 1

        total = 0.0
        for qt in query_tokens:
            idf = self._idf.get(qt, 0.0)
            f = tf.get(qt, 0)
            if f == 0:
                continue
            numer = f * (self.k1 + 1)
            denom = f + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
            total += idf * numer / denom
        return total

    def score_all(self, query_tokens: list[str]) -> list[float]:
        """Return BM25 scores for every document in the fitted corpus."""
        return [self.score(query_tokens, i) for i in range(len(self._doc_tokens))]

    def max_possible_score(self, query_tokens: list[str]) -> float:
        """Return the theoretical maximum BM25 score for *query_tokens*.

        Used for absolute normalization: dividing a raw score by this produces
        a 0-1 ratio representing how close a document is to a perfect match.

        Query tokens absent from the corpus contribute the corpus's maximum
        IDF as a penalty — this prevents partial matches from scoring as
        perfect matches when the other query tokens simply do not exist in
        the corpus.
        """
        if not self._idf:
            return 0.0
        max_idf = max(self._idf.values())
        return sum(self._idf.get(t, max_idf) for t in query_tokens)


# ---------------------------------------------------------------------------
# FuzzyEntitySearcher – now BM25-primary with SequenceMatcher fallback
# ---------------------------------------------------------------------------


class FuzzyEntitySearcher:
    """Entity search with BM25 keyword scoring and SequenceMatcher fallback."""

    def __init__(self, threshold: int = 60):
        """Initialize with fuzzy matching threshold."""
        self.threshold = threshold
        self.entity_cache: dict[str, Any] = {}

    def search_entities(
        self, entities: list[dict[str, Any]], query: str, limit: int = 10, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Search entities using BM25 scoring with SequenceMatcher typo fallback.

        Strategy:
          1. Tokenize every entity (entity_id + friendly_name) into a BM25 corpus.
          2. Score all documents with BM25.  Keep results above a positive threshold.
          3. If BM25 returns nothing, fall back to token-level SequenceMatcher on
             query tokens vs document tokens (catches single-character typos).

        Returns:
            Tuple of (paginated results list, total match count)
        """
        if not query or not entities:
            return [], 0

        query_lower = query.lower().strip()
        query_tokens = tokenize(query_lower)
        if not query_tokens:
            return [], 0

        # Build per-entity document: tokens from entity_id + friendly_name
        # + entity registry aliases (when callers enrich entities with the
        # ``_aliases`` key — see smart_search.smart_entity_search).
        docs: list[list[str]] = []
        meta: list[tuple[str, str, str, dict[str, Any], str]] = []  # eid, name, domain, attrs, state
        # Track which entities matched on alias (for `match_type="alias_match"`).
        alias_hit: list[set[str]] = []
        # Track ``hidden_by`` per entity so the score-penalty pass can
        # depress hidden hits without filtering them. Callers enrich via
        # the ``_hidden_by`` key — see smart_search.smart_entity_search.
        hidden_flags: list[Any] = []

        for entity in entities:
            entity_id = entity.get("entity_id", "")
            attributes = entity.get("attributes", {})
            friendly_name = attributes.get("friendly_name", entity_id)
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            state = entity.get("state", "unknown")

            id_tokens = tokenize(entity_id)
            name_tokens = tokenize(friendly_name)
            tokens = list(id_tokens + name_tokens)

            # Separator-stripped forms (concat tokens) so queries that
            # elide separators match — e.g. `bedlight` finds `light.bed_light`.
            tail = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
            tail_concat = _strip_separators(tail)
            if tail_concat:
                tokens.append(tail_concat)
            name_concat = _strip_separators(friendly_name)
            if name_concat and name_concat != tail_concat:
                tokens.append(name_concat)

            # Aliases (entity registry). Each alias contributes both its
            # tokenized form and its separator-stripped concat. We track
            # only the alias tokens that *aren't* already in id+name —
            # otherwise a query like `bed` would mislabel a friendly_name
            # match as `alias_match` whenever the entity also has a
            # `bed`-containing alias.
            id_name_tokens = set(id_tokens) | set(name_tokens)
            id_name_tokens.add(tail_concat)
            id_name_tokens.add(name_concat)
            entity_alias_tokens: set[str] = set()
            for alias in entity.get("_aliases", []) or []:
                if not isinstance(alias, str):
                    continue
                a_tokens = tokenize(alias)
                tokens.extend(a_tokens)
                for t in a_tokens:
                    if t not in id_name_tokens:
                        entity_alias_tokens.add(t)
                a_concat = _strip_separators(alias)
                if a_concat:
                    tokens.append(a_concat)
                    if a_concat not in id_name_tokens:
                        entity_alias_tokens.add(a_concat)

            docs.append(tokens)
            meta.append((entity_id, friendly_name, domain, attributes, state))
            alias_hit.append(entity_alias_tokens)
            hidden_flags.append(entity.get("_hidden_by"))

        # Fit BM25
        scorer = BM25Scorer()
        scorer.fit(docs)
        raw_scores = scorer.score_all(query_tokens)

        # Normalise against theoretical max (sum of IDFs) to produce absolute
        # scores in the 0-100 range. Empirical-max normalization would always
        # inflate the best match to 100 regardless of actual relevance, which
        # defeats the purpose of a threshold-based quality gate.
        theoretical_max = scorer.max_possible_score(query_tokens)
        matches: list[dict[str, Any]] = []

        if theoretical_max > 0:
            query_token_set = set(query_tokens)
            for i, raw in enumerate(raw_scores):
                if raw <= 0:
                    continue
                # Threshold gates the *raw* match quality so the option-c
                # contract holds: a hidden entity that genuinely matches
                # at threshold doesn't get penalised below it and silently
                # disappear. Penalty is applied only after the gate, so it
                # affects ranking but not visibility.
                raw_score = min(100, round(raw / theoretical_max * 100))
                if raw_score < self.threshold:
                    continue
                score = apply_hidden_penalty(raw_score, hidden_flags[i])
                eid, fname, domain, attrs, state = meta[i]
                # If any query token matched only on the alias haystack,
                # surface that to the caller via match_type — useful both
                # for telemetry and for the agent to know the friendly_name
                # alone wouldn't have led it here.
                hit_alias_tokens = query_token_set & alias_hit[i]
                if hit_alias_tokens:
                    match_type = "alias_match"
                else:
                    match_type = self._get_match_type(
                        eid, fname, domain, query_lower
                    )
                matches.append({
                    "entity_id": eid,
                    "friendly_name": fname,
                    "domain": domain,
                    "state": state,
                    "attributes": attrs,
                    "score": score,
                    "match_type": match_type,
                })

        # Tier-3 fallback: token-level SequenceMatcher only if BM25 scored
        # every document at zero. Firing the fallback when BM25 found valid
        # partial matches (just below threshold) would allow a character-level
        # match on the same token to inflate the score to 100, re-introducing
        # exactly the noise floor the new absolute normalization is fixing.
        bm25_found_any = any(raw > 0 for raw in raw_scores)
        if not matches and not bm25_found_any:
            matches = self._typo_fallback(
                query_tokens, query_lower, docs, meta, hidden_flags
            )

        matches.sort(key=lambda x: x["score"], reverse=True)
        total_matches = len(matches)
        return matches[offset:offset + limit], total_matches

    # -- private helpers -----------------------------------------------------

    def _typo_fallback(
        self,
        query_tokens: list[str],
        query_lower: str,
        docs: list[list[str]],
        meta: list[tuple[str, str, str, dict[str, Any], str]],
        hidden_flags: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Token-level SequenceMatcher fallback for typo correction.

        For multi-token queries, additionally requires coverage:
        at least half of the distinct query tokens must each have *some*
        doc token they ratio-match. Without this, a single-token
        accidental hit (e.g. ``garbage`` ≈ ``garage``) is enough to
        surface unrelated entities at score 92 from a 3-token query
        whose other two tokens have no doc relationship.
        """
        del query_lower  # parameter kept for API compatibility
        results: list[dict[str, Any]] = []
        distinct_query_tokens = list(dict.fromkeys(query_tokens))
        n_distinct = len(distinct_query_tokens)
        for i, doc_tokens in enumerate(docs):
            best_token_score = 0
            for qt in query_tokens:
                for dt in doc_tokens:
                    ratio = calculate_ratio(qt, dt)
                    best_token_score = max(best_token_score, ratio)

            if best_token_score < 75:  # stricter threshold for typo fallback
                continue

            # Multi-token coverage gate: how many distinct query tokens
            # have any doc token within the typo-fallback threshold?
            # A 3-token nonsense query that only one token explains
            # (coverage 1/3) is rejected; a single-token query is always
            # fully covered so unaffected.
            if n_distinct > 1:
                covered = 0
                for qt in distinct_query_tokens:
                    if any(calculate_ratio(qt, dt) >= 75 for dt in doc_tokens):
                        covered += 1
                if covered * 2 < n_distinct:  # < 50% coverage
                    continue

            eid, fname, domain, attrs, state = meta[i]
            entity_hidden = (
                hidden_flags[i] if hidden_flags is not None else None
            )
            # Apply the hidden penalty after the threshold gate above so
            # borderline hidden matches still surface (option-c contract);
            # the penalty only re-ranks them.
            score = apply_hidden_penalty(best_token_score, entity_hidden)
            results.append({
                "entity_id": eid,
                "friendly_name": fname,
                "domain": domain,
                "state": state,
                "attributes": attrs,
                "score": score,
                "match_type": "typo_fallback",
            })
        return results

    def _calculate_entity_score(
        self, entity_id: str, friendly_name: str, domain: str, query: str
    ) -> int:
        """Calculate a comprehensive fuzzy score for an entity name/domain.

        Actively used by ``ha_deep_search`` name scoring (automation, script,
        helper phases) to produce a score comparable to the legacy additive
        output those paths already rely on. Do not remove without migrating
        the deep-search callers to a BM25-based scheme.
        """
        score = 0

        # Exact matches get highest scores
        if query == entity_id.lower():
            score += 100
        elif query == friendly_name.lower():
            score += 95
        elif query == domain.lower():
            score += 90

        # Partial exact matches
        if query in entity_id.lower():
            score += 85
        if query in friendly_name.lower():
            score += 80

        # Fuzzy matching scores
        entity_id_ratio = calculate_ratio(query, entity_id.lower())
        friendly_ratio = calculate_ratio(query, friendly_name.lower())
        domain_ratio = calculate_ratio(query, domain.lower())

        # Partial ratio for substring matching
        entity_partial = calculate_partial_ratio(query, entity_id.lower())
        friendly_partial = calculate_partial_ratio(query, friendly_name.lower())

        # Token sort ratio for word order independence
        entity_token = calculate_token_sort_ratio(query, entity_id.lower())
        friendly_token = calculate_token_sort_ratio(query, friendly_name.lower())

        # Weight the scores (single floor to preserve original accumulation behavior)
        weighted = (
            max(entity_id_ratio, entity_partial, entity_token) * 0.7
            + max(friendly_ratio, friendly_partial, friendly_token) * 0.8
            + domain_ratio * 0.6
        )
        score += int(weighted)

        # Room/area keyword boosting
        room_keywords = [
            "salon",
            "chambre",
            "cuisine",
            "salle",
            "living",
            "bedroom",
            "kitchen",
        ]
        for keyword in room_keywords:
            if keyword in query and keyword in friendly_name.lower():
                score += 15

        # Device type boosting
        device_keywords = [
            "light",
            "switch",
            "sensor",
            "climate",
            "lumiere",
            "interrupteur",
        ]
        for keyword in device_keywords:
            if keyword in query and (
                keyword in domain or keyword in friendly_name.lower()
            ):
                score += 10

        return score

    def _get_match_type(
        self, entity_id: str, friendly_name: str, domain: str, query: str
    ) -> str:
        """Determine the type of match for user feedback."""
        if query == entity_id.lower():
            return "exact_id"
        elif query == friendly_name.lower():
            return "exact_name"
        elif query == domain.lower():
            return "exact_domain"
        elif query in entity_id.lower():
            return "partial_id"
        elif query in friendly_name.lower():
            return "partial_name"
        else:
            return "fuzzy_match"

    def search_by_area(
        self, entities: list[dict[str, Any]], area_query: str
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Group entities by area/room based on fuzzy matching.

        Args:
            entities: List of Home Assistant entity states
            area_query: Area/room name to search for

        Returns:
            Dictionary with area matches grouped by inferred area
        """
        area_matches: dict[str, list[dict[str, Any]]] = {}
        area_lower = area_query.lower().strip()

        for entity in entities:
            entity_id = entity.get("entity_id", "")
            attributes = entity.get("attributes", {})
            friendly_name = attributes.get("friendly_name", entity_id)

            # Check area_id attribute first
            if "area_id" in attributes:
                area_id = attributes["area_id"]
                if area_lower in area_id.lower():
                    if area_id not in area_matches:
                        area_matches[area_id] = []
                    area_matches[area_id].append(entity)
                    continue

            # Fuzzy match on friendly name for room inference
            area_score = calculate_partial_ratio(area_lower, friendly_name.lower())
            if area_score >= self.threshold:
                inferred_area = self._infer_area_from_name(friendly_name)
                if inferred_area not in area_matches:
                    area_matches[inferred_area] = []
                area_matches[inferred_area].append(entity)

        return area_matches

    def _infer_area_from_name(self, friendly_name: str) -> str:
        """Infer area/room from entity friendly name."""
        name_lower = friendly_name.lower()

        # Common French room names
        french_rooms = {
            "salon": "salon",
            "chambre": "chambre",
            "cuisine": "cuisine",
            "salle": "salle_de_bain",
            "bureau": "bureau",
            "garage": "garage",
            "jardin": "jardin",
            "terrasse": "terrasse",
        }

        # Common English room names
        english_rooms = {
            "living": "living_room",
            "bedroom": "bedroom",
            "kitchen": "kitchen",
            "bathroom": "bathroom",
            "office": "office",
            "garage": "garage",
            "garden": "garden",
            "patio": "patio",
        }

        all_rooms = {**french_rooms, **english_rooms}

        for keyword, room in all_rooms.items():
            if keyword in name_lower:
                return room

        return "unknown_area"

    def get_smart_suggestions(
        self, entities: list[dict[str, Any]], query: str
    ) -> list[str]:
        """
        Generate smart suggestions for failed searches.

        Args:
            entities: List of Home Assistant entity states
            query: Original search query

        Returns:
            List of suggested search terms
        """
        suggestions = []

        # Extract unique domains
        domains = set()
        areas = set()

        for entity in entities:
            entity_id = entity.get("entity_id", "")
            if "." in entity_id:
                domains.add(entity_id.split(".")[0])

            friendly_name = entity.get("attributes", {}).get("friendly_name", "")
            inferred_area = self._infer_area_from_name(friendly_name)
            if inferred_area != "unknown_area":
                areas.add(inferred_area)

        # Fuzzy match against domains
        domain_matches = extract_best_matches(query, domains, limit=3)
        suggestions.extend([match for match, score in domain_matches if score >= 60])

        # Fuzzy match against areas
        area_matches = extract_best_matches(query, areas, limit=3)
        suggestions.extend([match for match, score in area_matches if score >= 60])

        # Add common search patterns
        if not suggestions:
            suggestions.extend(
                [
                    "light",
                    "switch",
                    "sensor",
                    "climate",
                    "salon",
                    "chambre",
                    "cuisine",
                    "living",
                    "bedroom",
                    "kitchen",
                ]
            )

        return suggestions[:5]


def create_fuzzy_searcher(threshold: int = 60) -> FuzzyEntitySearcher:
    """Create a new fuzzy entity searcher instance."""
    return FuzzyEntitySearcher(threshold)


def calculate_ratio(query: str, value: str) -> int:
    """Return the similarity ratio (0-100) using SequenceMatcher."""
    return int(SequenceMatcher(None, query, value, autojunk=False).ratio() * 100)


def calculate_partial_ratio(query: str, value: str) -> int:
    """Return the best similarity score for any substring match."""
    if not query or not value:
        return 0

    shorter, longer = (query, value) if len(query) <= len(value) else (value, query)
    window = len(shorter)
    if window == 0:
        return 0

    best_score = 0
    for start in range(len(longer) - window + 1):
        substring = longer[start : start + window]
        best_score = max(best_score, calculate_ratio(shorter, substring))
        if best_score == 100:
            break

    return best_score


def calculate_token_sort_ratio(query: str, value: str) -> int:
    """Return similarity ratio after token sorting."""
    query_sorted = " ".join(sorted(query.split()))
    value_sorted = " ".join(sorted(value.split()))
    return calculate_ratio(query_sorted, value_sorted)


def extract_best_matches(
    query: str, choices: Iterable[str], limit: int = 3
) -> list[tuple[str, int]]:
    """Return the highest scoring matches for a query among choices."""
    scored_choices = [
        (choice, calculate_ratio(query, choice)) for choice in choices if choice
    ]
    scored_choices.sort(key=lambda item: item[1], reverse=True)
    return scored_choices[:limit]
