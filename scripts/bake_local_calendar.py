"""One-shot bake for an e2e local_calendar config entry.

Spins up HA against ``tests/initial_test_state/``, completes the
``local_calendar`` config flow via the REST config-entries API to register a
writable calendar entity, stops HA cleanly, and copies the resulting
``.storage/core.config_entries`` back into the seed dir. The entity-registry
entry and per-entry ``local_calendar.<entry_id>`` iCal file are deliberately
NOT copied — HA recreates them on boot from the config entry's ``storage_key``.

After this bake, the test container boots with a pre-registered
``calendar.local_e2e_test`` entity that supports event creation, unblocking
``tests/src/e2e/workflows/calendar/test_calendar.py::test_create_calendar_event``
which previously skipped on every CI run with
"Calendar event creation not available" because the only calendar entity in
the test env was the read-only demo calendar.

Usage:
    uv run python scripts/bake_local_calendar.py
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
SEED_DIR = REPO / "tests" / "initial_test_state"


def _load_test_constants() -> object:
    """Import tests/test_constants.py as a module without making tests/ a package."""
    constants_path = REPO / "tests" / "test_constants.py"
    spec = importlib.util.spec_from_file_location("_test_constants", constants_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module spec for {constants_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_constants = _load_test_constants()
TEST_TOKEN: str = _constants.TEST_TOKEN  # type: ignore[attr-defined]
HA_IMAGE: str = _constants.HA_TEST_IMAGE  # type: ignore[attr-defined]
# Includes "local" deliberately so `calendar._find_writable_calendar` finds
# this entity over the read-only demo calendar (it prefers entity_ids
# containing "local").
CALENDAR_NAME = "Local E2E Test"
HOST_PORT = 32789


def _docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True).strip()


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
    observed_calendars: list[str] = []
    for attempt in range(30):
        states_r = requests.get(
            f"{base_url}/api/states", timeout=5, headers=headers
        )
        states_r.raise_for_status()
        states = states_r.json()
        observed_calendars = [
            s["entity_id"] for s in states
            if s.get("entity_id", "").startswith("calendar.")
        ]
        local_cals = [
            eid for eid in observed_calendars
            if "demo" not in eid
            and "calendar_" not in eid  # demo entities use this prefix
        ]
        if local_cals:
            print(f"✓ Calendar entity registered after {attempt + 1}s: {local_cals[0]}")
            return str(entry_id)
        time.sleep(1)
    raise RuntimeError(
        f"local_calendar entity didn't register within 30s; "
        f"last observed calendar entities: {observed_calendars}"
    )


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
                HA_IMAGE,
            )
            print(f"✓ Started container {container_name} on :{HOST_PORT}")

            base_url = f"http://localhost:{HOST_PORT}"
            wait_for_api(base_url)
            create_local_calendar(base_url)

            # Graceful stop so HA flushes .storage writes. The REST stop kicks
            # off HA's shutdown; `docker stop -t 15` then waits up to 15s for
            # the container to exit before SIGKILL — enough time to flush
            # storage without a separate sleep.
            headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
            try:
                requests.post(
                    f"{base_url}/api/services/homeassistant/stop",
                    timeout=5,
                    headers=headers,
                )
            except requests.exceptions.RequestException as e:
                # Don't abort — docker stop -t 15 below still SIGTERMs HA — but
                # warn so the operator knows the bake may have flushed less than
                # expected if `core.config_entries` ends up empty.
                print(
                    f"  warning: graceful stop POST failed: {e}; "
                    f"falling back to docker stop -t 15",
                    file=sys.stderr,
                )
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
        if not src.exists():
            print(
                f"ERROR: bake produced no {src} — HA likely didn't flush "
                f".storage before shutdown",
                file=sys.stderr,
            )
            return 1
        shutil.copy2(src, dst)
        print(f"✓ Updated {dst}")

    print("DONE — review the diff before committing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
