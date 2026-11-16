"""Splitting the unit suite over CI runners (``UNIT_TEST_SHARD`` in pr.yml)."""

from zlib import crc32


def in_shard(nodeid: str, shard: str) -> bool:
    """Whether a test belongs to runner ``"<index>/<count>"`` (1-based).

    Chosen by the test's id, not its file: the tests of one file differ a lot
    in cost, so whole files do not split evenly.
    """
    index, count = (int(part) for part in shard.split("/"))
    if not 1 <= index <= count:
        raise ValueError(f"UNIT_TEST_SHARD must be <index>/<count>, got {shard!r}")
    return crc32(nodeid.encode()) % count == index - 1
