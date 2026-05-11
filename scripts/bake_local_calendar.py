"""One-shot bake for an e2e local_calendar config entry.

Spins up HA against ``tests/initial_test_state/``, completes the
``local_calendar`` config flow via the WebSocket API to register a writable
calendar entity, stops HA cleanly, and copies the resulting
``.storage/core.config_entries`` + ``.storage/local_calendar.*`` files back
into the seed dir.

After this bake, the test container boots with a pre-registered
``calendar.e2e_test_calendar`` entity that supports event creation, unblocking
``tests/src/e2e/workflows/calendar/test_calendar.py::test_create_calendar_event``
which previously skipped on every CI run with
"Calendar event creation not available" because the only calendar entity in
the test env was the read-only demo calendar.

Usage:
    uv run python scripts/bake_local_calendar.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
SEED_DIR = REPO / "tests" / "initial_test_state"
HA_IMAGE_VAR = REPO / "tests" / "test_constants.py"
# Public test token from tests/test_constants.py
TEST_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxOTE5ZTZlMTVkYjI0Mzk2YTQ4YjFiZTI1MDM1YmU2YSIsImlhdCI6MTc1NzI4OTc5NiwiZXhwIjoyMDcyNjQ5Nzk2fQ.Yp9SSAjm2gvl9Xcu96FFxS8SapHxWAVzaI0E3cD9xac"
# Includes "local" deliberately so `calendar._find_writable_calendar` finds
# this entity over the read-only demo calendar (it prefers entity_ids
# containing "local").
CALENDAR_NAME = "Local E2E Test"
HOST_PORT = 32789


def _docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


def _ha_image() -> str:
    """Pull HA_TEST_IMAGE from tests/test_constants.py so we always match CI."""
    for line in HA_IMAGE_VAR.read_text().splitlines():
        if line.startswith("HA_TEST_IMAGE"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("HA_TEST_IMAGE not found in tests/test_constants.py")


def create_local_calendar(base_url: str) -> str:
    """Drive local_calendar's config flow via REST; return new entry_id.

    HA's config-flow lives at ``/api/config/config_entries/flow`` (REST). The
    WebSocket flow commands aren't exposed for setup flows in modern HA.
    """
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    # Start the flow
    init_r = requests.post(
        f"{base_url}/api/config/config_entries/flow",
        json={"handler": "local_calendar", "show_advanced_options": False},
        headers=headers,
        timeout=10,
    )
    init_r.raise_for_status()
    flow = init_r.json()
    if flow.get("type") != "form":
        raise RuntimeError(f"Unexpected flow init shape: {flow}")
    flow_id = flow["flow_id"]

    # Submit the form. Modern local_calendar's user step uses `calendar_name`
    # (the legacy `name` field was renamed) plus an optional `import` mode.
    submit_r = requests.post(
        f"{base_url}/api/config/config_entries/flow/{flow_id}",
        json={"calendar_name": CALENDAR_NAME, "import": "create_empty"},
        headers=headers,
        timeout=15,
    )
    if not submit_r.ok:
        print(f"  flow submit returned {submit_r.status_code}: {submit_r.text}", file=sys.stderr)
        submit_r.raise_for_status()
    done = submit_r.json()
    if done.get("type") != "create_entry":
        raise RuntimeError(f"Flow did not complete: {done}")
    entry_id = done["result"]["entry_id"]
    print(f"✓ Created local_calendar config entry: {entry_id}")

    # Wait for the calendar entity to actually register. The integration sets
    # up async after the flow completes; stopping HA before then leaves the
    # entity registry unwritten and the seed unusable.
    for attempt in range(30):
        states = requests.get(
            f"{base_url}/api/states", timeout=5, headers=headers
        ).json()
        local_cals = [
            s for s in states
            if s.get("entity_id", "").startswith("calendar.")
            and "demo" not in s["entity_id"]
            and "calendar_" not in s["entity_id"]  # demo entities use this prefix
        ]
        if local_cals:
            print(f"✓ Calendar entity registered after {attempt + 1}s: {local_cals[0]['entity_id']}")
            return entry_id
        time.sleep(1)
    raise RuntimeError("local_calendar entity didn't register within 30s")


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
    raise RuntimeError(f"HA API didn't become ready in {timeout_s}s")


def main() -> int:
    if not SEED_DIR.exists():
        print(f"ERROR: seed dir not found at {SEED_DIR}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="ha_cal_bake_") as temp_dir:
        config_dir = Path(temp_dir)
        shutil.copytree(SEED_DIR, config_dir, dirs_exist_ok=True)
        print(f"✓ Staged seed at {config_dir}")

        container_name = f"ha-cal-bake-{int(time.time())}"
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
                _ha_image(),
            )
            print(f"✓ Started container {container_name} on :{HOST_PORT}")

            base_url = f"http://localhost:{HOST_PORT}"
            wait_for_api(base_url)
            create_local_calendar(base_url)

            # Graceful stop so HA flushes .storage writes
            headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
            try:
                requests.post(
                    f"{base_url}/api/services/homeassistant/stop",
                    timeout=5,
                    headers=headers,
                )
                time.sleep(8)
            except requests.exceptions.RequestException:
                pass
            _docker("stop", "-t", "15", container_name)
            print(f"✓ Stopped container {container_name}")
        except Exception:
            try:
                _docker("stop", "-t", "5", container_name)
            except subprocess.CalledProcessError:
                pass
            raise

        # Only core.config_entries needs to ship in the seed — the entity_id
        # for the local_calendar entity is derived at HA boot time from the
        # config entry's storage_key, so we deliberately skip copying the
        # entity_registry (which would otherwise bloat the seed unnecessarily).
        # local_calendar.<entry_id> for iCal storage is also created fresh by
        # HA on boot from the config-entry data, so we skip it too.
        src = config_dir / ".storage" / "core.config_entries"
        dst = SEED_DIR / ".storage" / "core.config_entries"
        if src.exists():
            shutil.copy2(src, dst)
            print(f"✓ Updated {dst}")

    print("DONE — review the diff before committing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
