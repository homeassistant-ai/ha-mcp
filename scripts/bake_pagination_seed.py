"""One-shot bake script for the e2e recorder pagination seed.

Spins up a fresh Home Assistant container against ``tests/initial_test_state/``,
drives ``input_number.e2e_pagination_seed`` through N distinct values, stops the
container cleanly so the recorder flushes, then copies the resulting
``home-assistant_v2.db`` back into ``tests/initial_test_state/``.

The result is a committed SQLite DB that ships with N pre-recorded state-change
rows for ``input_number.e2e_pagination_seed``. The companion timestamp-refresh
hook in ``tests/src/e2e/conftest.py`` shifts those rows forward each test
session so the rows always look "recent" to a ``24h`` history-window query.

Usage:
    uv run python scripts/bake_pagination_seed.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import websockets

REPO = Path(__file__).resolve().parents[1]
SEED_DIR = REPO / "tests" / "initial_test_state"
HA_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.4.1"
# Public test token from tests/test_constants.py
TEST_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxOTE5ZTZlMTVkYjI0Mzk2YTQ4YjFiZTI1MDM1YmU2YSIsImlhdCI6MTc1NzI4OTc5NiwiZXhwIjoyMDcyNjQ5Nzk2fQ.Yp9SSAjm2gvl9Xcu96FFxS8SapHxWAVzaI0E3cD9xac"
SEED_ENTITY = "input_number.e2e_pagination_seed"
NUM_VALUES = 10
HOST_PORT = 32788  # off the dev-harness 32769 to avoid clashes


def _docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


async def populate_recorder(base_url: str) -> None:
    """Connect to a running HA, set the seed input_number to N distinct values."""
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    ws_url = base_url.replace("http://", "ws://") + "/api/websocket"
    async with websockets.connect(ws_url) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": TEST_TOKEN}))
        ok = json.loads(await ws.recv())
        assert ok.get("type") == "auth_ok", ok

        msg_id = 1
        for value in range(1, NUM_VALUES + 1):
            await ws.send(
                json.dumps(
                    {
                        "id": msg_id,
                        "type": "call_service",
                        "domain": "input_number",
                        "service": "set_value",
                        "service_data": {"value": value},
                        "target": {"entity_id": SEED_ENTITY},
                    }
                )
            )
            msg_id += 1
            ack = json.loads(await ws.recv())
            if not ack.get("success", True):
                raise RuntimeError(f"call_service set_value={value} failed: {ack!r}")
            await asyncio.sleep(0.3)

        # Service call returning 200 doesn't guarantee state mutation if the
        # entity didn't accept the value — assert so we abort before
        # committing a useless DB. ``requests`` is blocking, so dispatch via
        # ``to_thread`` to keep the async function loop-safe (ASYNC210).
        state_r = await asyncio.to_thread(
            requests.get,
            f"{base_url}/api/states/{SEED_ENTITY}",
            timeout=5,
            headers=headers,
        )
        cur_state = state_r.json().get("state")
        expected = f"{float(NUM_VALUES):.1f}"
        assert cur_state == expected, (
            f"Entity {SEED_ENTITY} did not accept set_value: got {cur_state!r}, "
            f"expected {expected!r}. Bake aborted to avoid committing a useless DB."
        )
        print(f"✓ {SEED_ENTITY} state is {cur_state!r} (expected {expected!r})")

        # Default recorder commit_interval is 1s; sleep 5s for headroom.
        await asyncio.sleep(5)

        # Calling homeassistant.stop forces a synchronous recorder commit
        # before shutdown; `docker stop`'s SIGTERM path doesn't always flush.
        try:
            r = await asyncio.to_thread(
                requests.post,
                f"{base_url}/api/services/homeassistant/stop",
                timeout=5,
                headers=headers,
            )
            if r.status_code < 300:
                print("✓ Issued homeassistant.stop for clean recorder flush")
            else:
                print(
                    f"  homeassistant.stop returned HTTP {r.status_code}: {r.text[:200]}"
                )
        except requests.exceptions.RequestException as exc:
            # ConnectionError after stop is expected once HA tears down its API
            # — non-fatal; docker stop below is the fallback.
            print(
                f"  homeassistant.stop raised (likely HA already shutting down): {exc}"
            )
        await asyncio.sleep(8)
        print(f"✓ Set {SEED_ENTITY} to {NUM_VALUES} distinct values")


def wait_for_api(base_url: str, timeout_s: int = 90) -> None:
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    for attempt in range(timeout_s):
        try:
            r = requests.get(f"{base_url}/api/", timeout=3, headers=headers)
            if r.status_code == 200:
                print(f"✓ HA API ready after {attempt + 1}s")
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError(f"HA API at {base_url} didn't become ready in {timeout_s}s")


def wait_for_entity(base_url: str, entity_id: str, timeout_s: int = 30) -> None:
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    for attempt in range(timeout_s):
        try:
            r = requests.get(
                f"{base_url}/api/states/{entity_id}", timeout=3, headers=headers
            )
            if r.status_code == 200:
                print(f"✓ Entity {entity_id} registered after {attempt + 1}s")
                return
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError(
        f"Entity {entity_id} didn't register in {timeout_s}s "
        f"— check configuration.yaml has the input_number helper defined"
    )


def main() -> int:
    if not SEED_DIR.exists():
        print(f"ERROR: seed dir not found at {SEED_DIR}", file=sys.stderr)
        return 1

    # Stage in temp so HA doesn't mutate the source tree.
    with tempfile.TemporaryDirectory(prefix="ha_bake_") as temp_dir:
        config_dir = Path(temp_dir)
        shutil.copytree(SEED_DIR, config_dir, dirs_exist_ok=True)
        print(f"✓ Staged seed at {config_dir}")

        # Use the existing recorder DB if present — DO NOT WIPE IT. The
        # committed ``initial_test_state/home-assistant_v2.db`` may contain
        # rows other e2e tests depend on (e.g. seeded sun.sun, demo lights);
        # wiping would silently break them. HA will append our new rows to
        # whatever's already there.
        recorder_db = config_dir / "home-assistant_v2.db"

        container_name = f"ha-bake-{int(time.time())}"
        try:
            _docker(
                "run",
                "--rm",
                "-d",
                "--name",
                container_name,
                "-p",
                f"{HOST_PORT}:8123",
                "-v",
                f"{config_dir}:/config",
                "-e",
                "TZ=UTC",
                "--privileged",
                HA_IMAGE,
            )
            print(f"✓ Started container {container_name} on :{HOST_PORT}")

            base_url = f"http://localhost:{HOST_PORT}"
            wait_for_api(base_url)
            wait_for_entity(base_url, SEED_ENTITY)
            asyncio.run(populate_recorder(base_url))

            # Stop the container gracefully so the recorder flushes WAL → main DB.
            _docker("stop", "-t", "20", container_name)
            print(f"✓ Stopped container {container_name} (recorder flushed)")
        except Exception:
            try:
                _docker("stop", "-t", "5", container_name)
            except subprocess.CalledProcessError as cleanup_exc:
                print(
                    f"  cleanup `docker stop {container_name}` also failed: "
                    f"{cleanup_exc} — container may be leaked",
                    file=sys.stderr,
                )
            raise

        # After `docker stop` the main DB file may be missing recent committed
        # pages still in the `-wal` sidecar. `wal_checkpoint(TRUNCATE)` folds
        # them in so the copied file is self-contained.
        import sqlite3

        if not recorder_db.exists():
            print(
                f"ERROR: {recorder_db} was not created — HA never wrote a recorder DB",
                file=sys.stderr,
            )
            return 1

        conn = sqlite3.connect(str(recorder_db))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.commit()
            # Verify the checkpoint actually folded data into the main DB.
            # Modern HA recorder schema: entity_id lives in `states_meta`, joined
            # to `states` via `metadata_id`. The vestigial `states.entity_id`
            # column is CHAR(0) and always empty, so naive
            # `WHERE entity_id = ?` queries return 0.
            row_count = conn.execute(
                "SELECT COUNT(*) FROM states s "
                "JOIN states_meta sm ON s.metadata_id = sm.metadata_id "
                "WHERE sm.entity_id = ?",
                (SEED_ENTITY,),
            ).fetchone()[0]
            if row_count < NUM_VALUES:
                raise RuntimeError(
                    f"Bake produced only {row_count} rows for {SEED_ENTITY} "
                    f"(expected ≥{NUM_VALUES}). Recorder commit may have failed; "
                    f"refusing to overwrite the committed seed DB."
                )
            print(f"✓ WAL checkpointed; {row_count} states rows for {SEED_ENTITY}")
        finally:
            conn.close()

        dst = SEED_DIR / "home-assistant_v2.db"
        shutil.copy2(recorder_db, dst)
        print(f"✓ Copied baked DB to {dst} ({dst.stat().st_size:,} bytes)")

    print("DONE — commit tests/initial_test_state/home-assistant_v2.db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
