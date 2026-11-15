"""Filename aliases for edit snapshots whose stored identities name their owners."""

from __future__ import annotations

import hashlib
import re

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
_DIGEST_SUFFIX_RE = re.compile(r"-[0-9a-f]{8}$")


def entity_id_aliases(entity_id: str) -> list[str]:
    """Every filename stem a snapshot of ``entity_id`` may carry.

    Sanitised ids gained a digest suffix, so snapshots written before that
    sit under the bare sanitised name. Rotation and entity filtering match
    both, otherwise those older files would never be counted again: they
    would never be pruned, and would drop out of an entity-filtered listing.
    """
    current = safe_entity_id(entity_id)
    legacy = _SAFE_ID_RE.sub("_", entity_id).lstrip(".") or "_"
    aliases = [current, legacy]
    legacy_digest = _legacy_entity_id_digest(entity_id, legacy)
    if legacy_digest is not None:
        aliases.append(legacy_digest)
    return list(dict.fromkeys(aliases))


_MAX_ENTITY_STEM_LENGTH = 112
_ENTITY_ID_DIGEST_LENGTH = 8


def _entity_id_digest(entity_id: str) -> str:
    return hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[
        :_ENTITY_ID_DIGEST_LENGTH
    ]


def _legacy_entity_id_digest(entity_id: str, cleaned: str) -> str | None:
    if (
        cleaned != entity_id
        and len(cleaned) + _ENTITY_ID_DIGEST_LENGTH + 1 > _MAX_ENTITY_STEM_LENGTH
    ):
        return f"{cleaned}-{_entity_id_digest(entity_id)}"
    return None


def safe_entity_id(entity_id: str) -> str:
    """Sanitize an entity id for use in a filename.

    Replaces any character outside ``[A-Za-z0-9._-]`` with ``_``. Path
    separators get caught by this (the regex excludes both ``/`` and ``\\``).
    Strips leading dots to prevent dotfile collisions.

    Sanitizing is lossy, so ids that needed it get a short digest of the
    ORIGINAL appended. Blueprint domains (#2329) key on paths, where
    ``user/motion.yaml`` and ``user_motion.yaml`` both clean to
    ``user_motion.yaml`` -- without the digest they share one snapshot
    namespace, so captures in the same second overwrite each other and
    rotation counts both histories as one, able to delete the only restore
    point for a blueprint that was never written. Long stems are bounded to
    leave room for the domain, timestamp and atomic-write suffix. The complete
    original id remains in the snapshot payload.
    """
    if not entity_id:
        return "_"
    cleaned = _SAFE_ID_RE.sub("_", entity_id).lstrip(".")
    if not cleaned:
        return "_"
    if cleaned == entity_id and len(cleaned) <= _MAX_ENTITY_STEM_LENGTH:
        return cleaned
    digest = _entity_id_digest(entity_id)
    prefix_length = _MAX_ENTITY_STEM_LENGTH - len(digest) - 1
    return f"{cleaned[:prefix_length]}-{digest}"


def matches_listing_identity(payload_id: str | None, wanted: str) -> bool:
    """Whether a snapshot whose payload names ``payload_id`` is ``wanted``.

    The original id and its current or previous digest stem select an
    entity. A bare pre-digest stem is deliberately not accepted because
    it may belong to a differently-named entity.
    """
    if payload_id is None:
        return False
    if payload_id == wanted or safe_entity_id(payload_id) == wanted:
        return True
    cleaned = _SAFE_ID_RE.sub("_", payload_id).lstrip(".") or "_"
    return _legacy_entity_id_digest(payload_id, cleaned) == wanted


def might_be_legacy_digest(identity: str) -> bool:
    """A pre-cap digest can select files with a different, bounded stem."""
    return len(identity) > _MAX_ENTITY_STEM_LENGTH and bool(
        _DIGEST_SUFFIX_RE.search(identity)
    )
