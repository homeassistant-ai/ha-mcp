"""The recorder database E2E sessions boot on, as ``session_recorder_seed`` writes it.

Windows are measured from the moment Core closed the recorder run it wrote.
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from tests.src.recorder_seed import session_recorder_seed

from ._shards import in_shard

# Generating starts Core and writes ten days of data: about 20 seconds.
pytestmark = pytest.mark.slow

GENERATOR = "tests/src/unit/recorder_seed_generator.py::test_write_recorder_seed"

DAY = 24 * 3600
HOUR = 3600
FIVE_MINUTES = 300
REPOSITORY = Path(__file__).parents[3]
RESTORE_STATE = (
    REPOSITORY / "tests" / "initial_test_state" / ".storage" / "core.restore_state"
)


@pytest.fixture(scope="module")
def seed() -> Path:
    return session_recorder_seed()


@pytest.fixture(scope="module")
def db(seed: Path) -> Iterator[sqlite3.Connection]:
    # immutable: a reader that opens no journal leaves no sidecar of its own.
    connection = sqlite3.connect(f"{seed.as_uri()}?mode=ro&immutable=1", uri=True)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def written_at(db: sqlite3.Connection) -> float:
    (end,) = db.execute("SELECT MAX(end) FROM recorder_runs").fetchone()
    return datetime.fromisoformat(end).replace(tzinfo=UTC).timestamp()


def _state_times(db: sqlite3.Connection, entity_id: str) -> list[float]:
    rows = db.execute(
        "SELECT last_updated_ts FROM states JOIN states_meta USING (metadata_id)"
        " WHERE states_meta.entity_id = ?",
        (entity_id,),
    ).fetchall()
    return [ts for (ts,) in rows]


def _statistic_rows(
    db: sqlite3.Connection, table: str, since: float
) -> list[tuple[float, float]]:
    return db.execute(
        f"SELECT start_ts, sum FROM {table}"
        f" JOIN statistics_meta ON {table}.metadata_id = statistics_meta.id"
        " WHERE statistic_id = 'sensor.total_energy_kwh' AND start_ts >= ?"
        " ORDER BY start_ts",
        (since,),
    ).fetchall()


def test_copying_the_seed_alone_carries_every_committed_row(seed: Path) -> None:
    """Rows still in a -wal beside the file are lost when the file is copied alone."""
    assert [p.name for p in seed.parent.glob(f"{seed.name}-*")] == []
    # Header bytes 18 and 19 are 1 for a rollback journal and 2 for WAL, which
    # would grow the sidecars again as soon as Core opens the copy.
    assert seed.read_bytes()[18:20] == b"\x01\x01"


def test_the_recorder_run_is_closed_as_after_a_clean_shutdown(
    db: sqlite3.Connection,
) -> None:
    """Core treats a database whose last run is still open as not shut down cleanly."""
    assert db.execute("SELECT end FROM recorder_runs").fetchall() != []
    (unfinished,) = db.execute(
        "SELECT COUNT(*) FROM recorder_runs WHERE end IS NULL OR closed_incorrect"
    ).fetchone()
    assert unfinished == 0


def test_history_pagination_has_a_full_recent_page_and_an_older_one(
    db: sqlite3.Connection, written_at: float
) -> None:
    """With fewer recent rows the pagination E2E tests have no second page to read."""
    times = _state_times(db, "input_number.e2e_pagination_seed")
    assert sum(ts >= written_at - DAY for ts in times) >= 11
    assert any(ts < written_at - DAY for ts in times)


def test_the_sun_has_history_from_before_the_session(
    db: sqlite3.Connection, written_at: float
) -> None:
    """A test that reads sun.sun right after boot finds no rows without it."""
    assert any(ts >= written_at - DAY for ts in _state_times(db, "sun.sun"))


def test_no_state_is_old_enough_for_the_recorder_to_purge(
    db: sqlite3.Connection, written_at: float
) -> None:
    """Core's default purge_keep_days is 10: an older row vanishes from history."""
    (oldest,) = db.execute("SELECT MIN(last_updated_ts) FROM states").fetchone()
    assert oldest >= written_at - 10 * DAY


def test_hourly_energy_statistics_for_a_week_have_no_gaps_and_a_rising_sum(
    db: sqlite3.Connection, written_at: float
) -> None:
    """A missing hour or two rows in one hour reach every statistics period above it."""
    rows = _statistic_rows(db, "statistics", written_at - 7 * DAY)
    starts = [start for start, _ in rows]
    assert starts, "no hourly statistics in the last 7 days"
    assert starts[0] < written_at - 7 * DAY + HOUR
    assert starts[-1] >= written_at - 2 * HOUR
    assert all(later - earlier == HOUR for earlier, later in pairwise(starts))
    sums = [total for _, total in rows]
    assert all(isinstance(total, float) for total in sums)
    assert all(later >= earlier for earlier, later in pairwise(sums))


def test_five_minute_statistics_run_unbroken_to_the_last_complete_period(
    db: sqlite3.Connection, written_at: float
) -> None:
    """A database Core creates now has no statistics for its latest period.

    Core marks that period compiled at creation; at :55 the whole hour is lost.
    """
    starts = [
        start
        for start, _ in _statistic_rows(db, "statistics_short_term", written_at - DAY)
    ]
    assert starts[-1] == written_at // FIVE_MINUTES * FIVE_MINUTES - FIVE_MINUTES
    assert all(later - earlier == FIVE_MINUTES for earlier, later in pairwise(starts))


def test_energy_totals_end_where_the_live_sensors_resume(
    db: sqlite3.Connection,
) -> None:
    """A total that ends elsewhere shows as a jump in the booted instance's statistics."""
    energy = ("sensor.total_energy_kwh", "sensor.total_energy_mwh")
    restored = {
        entry["state"]["entity_id"]: entry["extra_data"]["native_value"]
        for entry in json.loads(RESTORE_STATE.read_text(encoding="utf-8"))["data"]
        if entry["state"]["entity_id"] in energy
    }
    for entity_id in energy:
        (last,) = db.execute(
            "SELECT state FROM states JOIN states_meta USING (metadata_id)"
            " WHERE states_meta.entity_id = ? ORDER BY last_updated_ts DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        assert float(last) == restored[entity_id]


def _session(script: str, tmp_path: Path, **env: str) -> subprocess.Popen[str]:
    """A pytest session's process of its own, whose temp directory is ``tmp_path``."""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=REPOSITORY,
        env=os.environ | {"TMPDIR": str(tmp_path)} | env,
        stdout=subprocess.PIPE,
        text=True,
        # Its own process group, which also holds the generator it starts.
        start_new_session=True,
    )


def _printed(sessions: list[subprocess.Popen[str]]) -> list[str]:
    try:
        printed = [session.communicate(timeout=300)[0].strip() for session in sessions]
    finally:
        for session in sessions:
            if session.poll() is None:
                os.killpg(session.pid, signal.SIGKILL)
                session.communicate()
    assert [session.returncode for session in sessions] == [0] * len(sessions)
    return printed


def test_a_failed_generation_is_reported_again_without_generating_again(
    tmp_path: Path,
) -> None:
    """Each xdist worker repeating a failed generation multiplies a minutes-long failure."""
    script = """
import os
from tests.src.recorder_seed import session_recorder_seed

os.environ["PYTHONHASHSEED"] = "not-a-seed"
for attempt in range(2):
    try:
        session_recorder_seed()
    except RuntimeError as error:
        print("PYTHONHASHSEED" in str(error))
    # A second generation would now succeed, and take as long as the first.
    os.environ.pop("PYTHONHASHSEED", None)
"""
    assert _printed([_session(script, tmp_path)]) == ["True\nTrue"]


def test_two_workers_of_a_sharded_ci_session_share_one_generated_file(
    tmp_path: Path,
) -> None:
    """A worker that generates again replaces the file another worker is copying.

    The generator is one test of the unit tree, so the workers' own shard must
    not reach it: this one would deselect it.
    """
    script = (
        "import os; from tests.src.recorder_seed import session_recorder_seed; "
        "print(os.stat(session_recorder_seed()).st_ino)"
    )
    shard = next(shard for shard in ("1/2", "2/2") if not in_shard(GENERATOR, shard))
    first, second = _printed(
        [
            _session(
                script,
                tmp_path,
                PYTEST_XDIST_TESTRUNUID="shared-run",
                UNIT_TEST_SHARD=shard,
            )
            for _ in range(2)
        ]
    )
    assert first == second != ""
