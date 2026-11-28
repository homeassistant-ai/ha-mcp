"""Filename aliases for edit snapshots whose stored identities name their owners."""

from __future__ import annotations

import hashlib
import re

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
_DIGEST_STEM_RE = re.compile(r"[A-Za-z0-9._-]+-[0-9a-f]{8}")


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


def listing_ids(entity_id: str, domain: str | None) -> tuple[str, ...]:
    """The stored ids ``entity_id`` stands for in a ``domain`` snapshot.

    ``domain`` None means a listing across every domain. A group's snapshots
    are kept under its object id, but captures made before by
    ``group.<object_id>`` kept the prefix, so a group's id takes both forms.
    """
    if domain not in (None, "group"):
        return (entity_id,)
    bare = entity_id.removeprefix("group.")
    return (bare, f"group.{bare}")


def listing_aliases(entity_id: str, domain: str | None) -> list[str]:
    """Every filename stem a listing filtered by ``entity_id`` takes in ``domain``."""
    ids = listing_ids(entity_id, domain)
    return list(dict.fromkeys(a for i in ids for a in entity_id_aliases(i)))


def matches_listing_identity(
    payload_id: str | None, wanted: str, file_stem: str, domain: str
) -> bool:
    """Whether a ``domain`` snapshot whose payload names ``payload_id`` is ``wanted``.

    ``domain`` is the snapshot's own; a group snapshot also matches ``wanted``
    with or without its ``group.`` prefix (``listing_ids``). The original id
    selects the complete history. A digest selector includes files written
    under either generation of digest naming.
    """
    return any(
        _matches_one(payload_id, w, file_stem) for w in listing_ids(wanted, domain)
    )


def _matches_one(payload_id: str | None, wanted: str, file_stem: str) -> bool:
    if payload_id is None:
        return False
    if payload_id == wanted:
        return True
    current = safe_entity_id(payload_id)
    cleaned = _SAFE_ID_RE.sub("_", payload_id).lstrip(".") or "_"
    previous = _legacy_entity_id_digest(payload_id, cleaned)
    # Before the cap, already-safe ids were written verbatim at any length.
    stems = (current, previous, payload_id)
    return wanted in stems and file_stem in stems


def might_name_multiple_stems(identity: str) -> bool:
    """A digest selector can refer to older filenames only the payload identifies."""
    return (
        len(identity) >= _MAX_ENTITY_STEM_LENGTH
        and _DIGEST_STEM_RE.fullmatch(identity) is not None
    )
