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
        # Auth handshake
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
            await ws.recv()  # ignore ack
            await asyncio.sleep(0.3)

        # Verify the state actually changed (sanity check; service call returning
        # 200 doesn't guarantee state mutation if e.g. the entity didn't accept
        # the value).
        state_r = requests.get(
            f"{base_url}/api/states/{SEED_ENTITY}", timeout=5, headers=headers
        )
        cur_state = state_r.json().get("state")
        print(f"  current state of {SEED_ENTITY}: {cur_state!r} (expected '{NUM_VALUES}.0')")

        # Force a recorder commit so the rows hit disk before we stop the
        # container. The default commit_interval is 1s; ask for 5s of buffer.
        await asyncio.sleep(5)

        # Trigger a graceful HA shutdown via the service API. `docker stop`
        # sends SIGTERM but HA's recorder commit-on-shutdown isn't always
        # reliable; calling homeassistant.stop is the documented clean path.
        try:
            requests.post(
                f"{base_url}/api/services/homeassistant/stop",
                timeout=5,
                headers=headers,
            )
            print("✓ Issued homeassistant.stop for clean recorder flush")
        except requests.exceptions.RequestException as exc:
            # Non-fatal: docker stop below is the fallback.
            print(f"  homeassistant.stop returned: {exc}")
        # Give HA a moment to actually shut down before docker stop fires.
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

    # Stage the seed in a temp dir so we don't mutate the source tree while HA runs.
    with tempfile.TemporaryDirectory(prefix="ha_bake_") as temp_dir:
        config_dir = Path(temp_dir)
        # copytree wants the dest to NOT exist; use dirs_exist_ok to allow it
        shutil.copytree(SEED_DIR, config_dir, dirs_exist_ok=True)
        print(f"✓ Staged seed at {config_dir}")

        # Use the existing recorder DB if present — DO NOT WIPE IT. The
        # committed ``initial_test_state/home-assistant_v2.db`` may contain
        # rows other e2e tests depend on (e.g. seeded sun.sun, demo lights);
        # wiping would silently break them. HA will append our new rows to
        # whatever's already there.
        recorder_db = config_dir / "home-assistant_v2.db"

        # Start a one-shot HA container.
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
            # Try to clean up if we left the container running.
            try:
                _docker("stop", "-t", "5", container_name)
            except subprocess.CalledProcessError:
                pass
            raise

        # Copy the post-bake DB into the source seed. HA uses SQLite WAL mode;
        # `docker stop` doesn't always merge the WAL back into the main DB, so
        # the standalone home-assistant_v2.db file can be near-empty even when
        # data is fully committed to the WAL sibling. Checkpoint the WAL into
        # the main DB ourselves so the committed DB is self-contained.
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
            # Verify the checkpoint actually folded data into the main DB
            row_count = conn.execute(
                "SELECT COUNT(*) FROM states WHERE entity_id = ? OR state_id IN ("
                "  SELECT state_id FROM states_meta WHERE entity_id = ?)",
                (SEED_ENTITY, SEED_ENTITY),
            ).fetchone()[0] if any(
                r[0] == "states_meta"
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ) else conn.execute(
                "SELECT COUNT(*) FROM states WHERE entity_id = ?",
                (SEED_ENTITY,),
            ).fetchone()[0]
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
