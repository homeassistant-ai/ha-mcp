"""Readiness gates for the session Home Assistant, their timings and the failure reports."""

import json
import logging
import time
from collections.abc import Generator
from typing import Any

import pytest
import requests
from testcontainers.core.container import DockerContainer

logger = logging.getLogger(__name__)


# Module-level collection of readiness-gate timings, per process. Workers
# write to ``_READINESS_TIMINGS``; pytest_sessionfinish hands their lists
# to the master via xdist's ``workeroutput`` channel, and the master
# aggregates into ``_ALL_READINESS_TIMINGS`` in pytest_testnodedown. The
# pytest_terminal_summary hook then renders the aggregate to the
# master's terminalreporter, which writes outside the pytest-xdist
# capture buffer (refs #366).
_READINESS_TIMINGS: list[dict[str, Any]] = []
_ALL_READINESS_TIMINGS: list[dict[str, Any]] = []
# Home Assistant logs taken when a non-fatal gate gives up, routed the same
# way: a WARNING logged from a session fixture is shown only if the test that
# set it up fails, so the session would otherwise carry on with no record.
_READINESS_DIAGNOSTICS: list[str] = []
_ALL_READINESS_DIAGNOSTICS: list[str] = []

# The session's Home Assistant container on the testcontainer lanes, so a
# failing test can attach the Home Assistant log from its own time window.
_HA_CONTAINER_KEY = pytest.StashKey[DockerContainer]()
# Bounds the attached log when one fault fails many tests at once.
_FAILED_TEST_LOG_TAIL = 200


def _log_readiness_timing(gate: str, elapsed_s: float, **extras: Any) -> None:
    """Record a fixture-side readiness-gate timing data point.

    The data points are routed to the master process and rendered at
    session end by ``pytest_terminal_summary``. Direct ``sys.stderr``
    writes don't survive pytest-xdist's per-worker capture buffer, so
    going through pytest's own reporting plumbing is the reliable path.

    The current ``core_state`` emission includes ``entries_loaded`` /
    ``entries_total`` / ``snapshot_ok`` from ``_snapshot_config_entries``
    at the trip moment. ``pytest_terminal_summary`` warns when any
    ``snapshot_ok`` sample shows ``entries_loaded < entries_total`` —
    that drift means a slow integration finished ``async_setup_entry``
    after ``CoreState.RUNNING`` was set and a follow-up gate is
    justified. Gate-instrumentation history: introduced in #1310 and
    iterated through #1346 / #1369; consolidated onto the single
    ``CoreState.RUNNING`` check in the PR referencing #366
    (Ilya0527 2026-05-18 thread for the structural rationale).
    """
    _READINESS_TIMINGS.append({"gate": gate, "elapsed_s": elapsed_s, **extras})


def _container_log(container: DockerContainer, **log_kwargs: Any) -> str:
    """The container's log, or why it could not be read."""
    import docker as _docker

    try:
        raw = container.get_wrapped_container().logs(**log_kwargs)
    except _docker.errors.DockerException as exc:
        return f"docker logs failed: {type(exc).__name__}: {exc}"
    return raw.decode("utf-8", errors="ignore")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    # A tool failure often has its real cause only in the Home Assistant log
    # (a webhook 502 names the upstream error there and nowhere else).
    report = yield
    if report.when == "call" and report.failed:
        container = item.config.stash.get(_HA_CONTAINER_KEY, None)
        if container is not None:
            report.sections.append(
                (
                    "Home Assistant log during this test",
                    _container_log(
                        container, since=call.start, tail=_FAILED_TEST_LOG_TAIL
                    ),
                )
            )
    return report


def pytest_sessionfinish(session, exitstatus):
    """xdist worker hook: hand collected timings up to the master.

    ``config.workeroutput`` only exists on workers; on the master (or
    when running without xdist) the attribute is missing, and the local
    list is read directly by ``pytest_terminal_summary`` instead.
    """
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None and _READINESS_TIMINGS:
        workeroutput["readiness_timings"] = list(_READINESS_TIMINGS)
    if workeroutput is not None and _READINESS_DIAGNOSTICS:
        workeroutput["readiness_diagnostics"] = list(_READINESS_DIAGNOSTICS)


def pytest_testnodedown(node, error):
    """xdist master hook: collect a finished worker's timings.

    Called once per worker as it shuts down. ``error`` is non-None when
    the worker crashed — we still try to drain whatever it managed to
    record.
    """
    workeroutput = getattr(node, "workeroutput", {})
    _ALL_READINESS_TIMINGS.extend(workeroutput.get("readiness_timings", []))
    _ALL_READINESS_DIAGNOSTICS.extend(workeroutput.get("readiness_diagnostics", []))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Master-side hook: render collected timings as a terminal section.

    Falls back to the local list when running without xdist (no
    ``pytest_testnodedown`` fires in that mode, so ``_ALL_*`` stays empty).
    """
    diagnostics = _ALL_READINESS_DIAGNOSTICS or _READINESS_DIAGNOSTICS
    if diagnostics:
        terminalreporter.section("Readiness gate diagnostics")
        for dump in diagnostics:
            terminalreporter.write_line(dump)
    timings = _ALL_READINESS_TIMINGS or _READINESS_TIMINGS
    if not timings:
        return
    terminalreporter.section("Readiness gate timings")
    for point in timings:
        parts = []
        for key, value in point.items():
            if isinstance(value, float):
                parts.append(f"{key}={value:.2f}")
            else:
                parts.append(f"{key}={value}")
        terminalreporter.write_line("[READINESS_GATE_TIMING] " + " ".join(parts))

    # Drift check (warn-only): the ``_log_readiness_timing`` docstring
    # promises this invariant — if any ``core_state`` sample shows
    # ``entries_loaded < entries_total`` at trip time, a slow integration
    # finished ``async_setup_entry`` after ``CoreState.RUNNING`` was set
    # and a follow-up gate would be justified. ``snapshot_ok=False``
    # samples are skipped (sentinel zeros, not real data).
    drift_samples = [
        p
        for p in timings
        if p.get("gate") == "core_state"
        and p.get("snapshot_ok")
        and p.get("entries_loaded", 0) < p.get("entries_total", 0)
    ]
    if drift_samples:
        culprits = sorted(
            {p.get("unloaded") for p in drift_samples if p.get("unloaded")}
        )
        terminalreporter.write_line(
            f"⚠️ [READINESS_GATE_DRIFT] {len(drift_samples)} core_state sample(s) "
            f"with entries_loaded < entries_total — a slow integration "
            f"finished async_setup_entry after CoreState.RUNNING; "
            f"the entries_loaded follow-up gate absorbed the wait (see its "
            f"timing line; a timed_out=True line there means it did not)."
            + (f" Not loaded: {'; '.join(culprits)}" if culprits else "")
        )


def _wait_for_ha_api_ready(
    base_url: str,
    headers: dict[str, str],
    timeout: int,
) -> bool:
    """Poll ``/api/`` for HTTP 200. Returns True on success, False on timeout.

    Called from the initial-boot path in ``ha_container_with_fresh_config``;
    the helper extraction is preserved so a future call site (e.g. a
    bounded retry after a real recoverable flake surfaces in the dump)
    can re-use the same readiness contract without duplicating the
    polling loop.

    NOTE: ``/api/`` 200 is HA's *liveness* signal (HTTP component up),
    not its *readiness* signal. The readiness gate that asserts every
    integration's ``async_setup_entry`` has completed lives in
    ``_wait_for_core_state_running`` below.
    """

    # Wall-clock-bound polling: ``for attempt in range(timeout)`` would let a
    # single slow request (5s HTTP timeout) plus the 1s sleep stretch each
    # iteration to ~6s, so a hung server could keep the loop running for up
    # to ``6 * timeout`` seconds instead of ``timeout``. The monotonic-based
    # cap enforces the budget the caller asked for.
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        try:
            response = requests.get(f"{base_url}/api/", timeout=5, headers=headers)
            if response.status_code == 200:
                elapsed = int(time.monotonic() - start_time)
                logger.info(f"🏠 Home Assistant API ready after {elapsed}s")
                return True
        except requests.exceptions.RequestException:
            # Boot-phase polling: HA API not up yet — retry until timeout (#1266).
            pass
        time.sleep(1)
    return False


def _snapshot_config_entries(
    base_url: str,
    headers: dict[str, str],
    *,
    timeout: float = 5.0,
) -> tuple[int, int, bool, str]:
    """Return ``(loaded_count, total_count, snapshot_ok, unloaded)`` from
    ``/api/config/config_entries/entry``.

    ``unloaded`` names the entries that are NOT ``state == "loaded"`` as a
    comma-joined ``domain:state`` string (capped at 5), so a drift sample
    identifies the culprit directly — count-only telemetry left the
    2033-arm ``11/12`` incidents unattributable without container logs.

    Used by ``_wait_for_core_state_running`` to capture the entry-state
    snapshot at the trip moment (and on timeout) so post-merge data can
    verify whether ``CoreState.RUNNING`` actually closes the race for
    slow integrations or whether late-binding entries are still loading
    when the gate exits.

    ``snapshot_ok=False`` signals that the count fields are sentinel
    ``(0, 0)`` rather than a legitimately empty entries list — the
    distinction matters for the ``pytest_terminal_summary`` drift check,
    which would otherwise read both sides as 0 on a persistently-broken
    endpoint and never fire. The endpoint hit is best-effort
    instrumentation, not a hard requirement.
    """

    try:
        resp = requests.get(
            f"{base_url}/api/config/config_entries/entry",
            timeout=timeout,
            headers=headers,
        )
        if resp.status_code != 200:
            logger.debug(f"snapshot_config_entries: HTTP {resp.status_code}")
            return 0, 0, False, ""
        entries = resp.json()
        if not isinstance(entries, list):
            logger.debug(
                f"snapshot_config_entries: unexpected body type {type(entries).__name__}"
            )
            return 0, 0, False, ""
        total = len(entries)
        loaded = sum(
            1 for e in entries if isinstance(e, dict) and e.get("state") == "loaded"
        )
        unloaded = ",".join(
            f"{e.get('domain', '?')}:{e.get('state', '?')}"
            for e in entries
            if isinstance(e, dict) and e.get("state") != "loaded"
        )[:200]
        return loaded, total, True, unloaded
    except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
        logger.debug(f"snapshot_config_entries: {type(exc).__name__}: {exc}")
        return 0, 0, False, ""


# Bounded budget for the entries_loaded follow-up gate. HACS (the only entry
# observed lagging — it fetches remote data during setup) typically finishes
# seconds after CoreState.RUNNING; 60s is generous without stalling a genuinely
# broken container for long.
_ENTRIES_LOADED_TIMEOUT = 60


def _wait_for_entries_loaded(
    container: DockerContainer,
    base_url: str,
    headers: dict[str, str],
    timeout: int = _ENTRIES_LOADED_TIMEOUT,
) -> None:
    """Follow-up readiness gate: wait for every config entry to reach ``loaded``.

    ``CoreState.RUNNING`` only means every ``async_setup_entry`` was
    DISPATCHED; a slow one can still be running. The drift telemetry
    repeatedly caught the seeded HACS entry ``not_loaded`` at trip time
    (#2033's arm lanes, #2040's x86 lane), which the OptionsFlow probe tests
    then observe as all-empty options — the #1245 regression signature —
    while a sibling test probing the same entry seconds later passes. This
    gate closes exactly that window.

    Bounded and non-fatal: on timeout the unloaded entries and the Home
    Assistant log so far go to the terminal summary and the session proceeds.
    A genuinely broken entry should fail its own tests with the timing line as
    the named cause, not abort the whole run.
    """
    start = time.monotonic()
    while True:
        loaded, total, ok, unloaded = _snapshot_config_entries(base_url, headers)
        elapsed = time.monotonic() - start
        if ok and total > 0 and loaded >= total:
            _log_readiness_timing(
                "entries_loaded",
                elapsed,
                entries_loaded=loaded,
                entries_total=total,
                snapshot_ok=ok,
            )
            return
        if elapsed >= timeout:
            _READINESS_DIAGNOSTICS.append(
                f"entries_loaded gate timed out after {elapsed:.0f}s: "
                f"{loaded}/{total} loaded"
                + (f" (not loaded: {unloaded})" if unloaded else "")
                + ". Home Assistant log so far:\n"
                + _container_log(container)
            )
            _log_readiness_timing(
                "entries_loaded",
                elapsed,
                entries_loaded=loaded,
                entries_total=total,
                snapshot_ok=ok,
                timed_out=True,
                **({"unloaded": unloaded} if unloaded else {}),
            )
            return
        time.sleep(1)


def _wait_for_core_state_running(
    base_url: str,
    headers: dict[str, str],
    timeout: int,
) -> tuple[bool, float, str, int, int, bool, str]:
    """Poll ``/api/core/state`` until ``state == "RUNNING"``.

    Returns ``(success, elapsed_s, last_state, entries_loaded,
    entries_total, snapshot_ok, entries_unloaded)``. ``snapshot_ok=False``
    flags that the entries fields are sentinel zeros rather than real
    data; ``entries_unloaded`` names the not-loaded entries as
    ``domain:state`` pairs (empty when all loaded).

    HA Core's ``APICoreStateView`` (``homeassistant/components/api/__init__.py``)
    is the documented Supervisor-facing readiness endpoint: it reports the
    ``CoreState`` enum value, which only transitions to ``"RUNNING"`` once
    every integration's ``async_setup_entry`` has been dispatched (or hit
    the 300s ``SLOW_SETUP_MAX_WAIT`` per-domain timeout from
    ``homeassistant/setup.py``). ``/api/`` 200 by contrast is HA's
    liveness signal — the HTTP component finishes setup early in bootstrap,
    before most integrations.

    Background (#366 thread, Ilya0527 2026-05-18): polling ``/api/`` then
    counting components / entities / services across separate gates was
    racing ``async_setup_entry`` for slow integrations. This helper
    replaces five such gates with a single ``CoreState.RUNNING`` check.
    A tight ``sun.sun`` state poll is still kept inline at the call site
    because sun's first periodic position computation runs as a scheduled
    task after ``async_setup_entry`` returns, so ``RUNNING`` does not
    strictly imply ``sun.sun != "unknown"``.

    Also captures ``/api/config/config_entries/entry`` at the trip moment
    via ``_snapshot_config_entries`` to emit ``entries_loaded`` /
    ``entries_total`` alongside the success signal. The post-merge data
    window then verifies that ``RUNNING`` actually closes the race: any
    run where ``entries_loaded < entries_total`` at trip time means a
    slow integration finished its ``async_setup_entry`` after
    ``CoreState.RUNNING`` was set, and a follow-up gate would be
    justified.
    """

    start_time = time.monotonic()
    last_state = "<no response>"
    while time.monotonic() - start_time < timeout:
        try:
            response = requests.get(
                f"{base_url}/api/core/state", timeout=2, headers=headers
            )
            if response.status_code == 200:
                # Parse before stamping last_state — a JSONDecodeError on
                # a 200 body would otherwise leave last_state misreporting
                # "HTTP 200" when the actual cause was malformed JSON,
                # which the except below correctly attributes to the
                # exception class.
                state_value = response.json().get("state", "<unknown>")
                last_state = state_value
                if state_value == "RUNNING":
                    elapsed = time.monotonic() - start_time
                    entries_loaded, entries_total, snapshot_ok, entries_unloaded = (
                        _snapshot_config_entries(base_url, headers)
                    )
                    logger.info(
                        f"🏃 CoreState.RUNNING after {elapsed:.1f}s "
                        f"(entries loaded: {entries_loaded}/{entries_total}"
                        f"{f', not loaded: {entries_unloaded}' if entries_unloaded else ''}"
                        f"{'' if snapshot_ok else ', snapshot unavailable'})"
                    )
                    return (
                        True,
                        elapsed,
                        state_value,
                        entries_loaded,
                        entries_total,
                        snapshot_ok,
                        entries_unloaded,
                    )
            else:
                # Surface HTTP status as a fallback ``last_state`` so a
                # persistent non-200 (e.g. 503 during HA boot) shows up
                # in the timeout ``pytest.fail`` message instead of the
                # initial ``"<no response>"``.
                last_state = f"HTTP {response.status_code}"
        except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
            last_state = type(exc).__name__
            logger.debug(f"core_state check failed: {exc}")
        time.sleep(1)

    # Final snapshot for the failure-diagnostics path — HA is known-bad
    # by this point, so use a tight 2s timeout rather than the default 5s
    # to keep teardown responsive.
    entries_loaded, entries_total, snapshot_ok, entries_unloaded = (
        _snapshot_config_entries(base_url, headers, timeout=2.0)
    )
    return (
        False,
        time.monotonic() - start_time,
        last_state,
        entries_loaded,
        entries_total,
        snapshot_ok,
        entries_unloaded,
    )


def _dump_services_diagnostics(base_url: str, headers: dict[str, str]) -> None:
    """Log the ``/api/services`` domain snapshot for a readiness-gate failure."""
    # /api/services snapshot — distinguishes "domain absent" from
    # "request errored at timeout edge".
    try:
        svc_resp = requests.get(f"{base_url}/api/services", timeout=5, headers=headers)
        if svc_resp.status_code == 200:
            domains = sorted(
                {s.get("domain") for s in svc_resp.json() if s.get("domain")}
            )
            logger.warning(f"  /api/services: {len(domains)} domains: {domains}")
        else:
            logger.warning(
                f"  /api/services: HTTP {svc_resp.status_code} {svc_resp.text[:200]}"
            )
    except Exception as exc:  # noqa: BLE001
        # Broad catch by design: this is a diagnostic dump, and an
        # unexpected exception class (e.g. SSL error subclass, unicode
        # decode of an HTML 5xx body) should not abort the remaining
        # captures below. The per-capture try/except scope ensures any
        # single failure is logged without losing the others.
        logger.warning(f"  /api/services: request failed: {type(exc).__name__}: {exc}")


def _dump_config_entries_diagnostics(
    base_url: str, headers: dict[str, str], config_entry_domain: str | None
) -> None:
    """Log the config-entries snapshot for a readiness-gate failure."""
    # /api/config/config_entries/entry — surfaces the entry's state
    # ('loaded' / 'setup_retry' / 'setup_error' / 'not_loaded' / etc.).
    # Distinguishes "HA never imported the entry" from "HA imported but
    # async_setup_entry raised".
    try:
        entries_resp = requests.get(
            f"{base_url}/api/config/config_entries/entry",
            timeout=5,
            headers=headers,
        )
        if entries_resp.status_code == 200:
            entries = entries_resp.json()
            entry_domains = sorted(
                {
                    e.get("domain")
                    for e in entries
                    if isinstance(e, dict) and e.get("domain")
                }
            )
            if config_entry_domain:
                matching = [
                    e
                    for e in entries
                    if isinstance(e, dict) and e.get("domain") == config_entry_domain
                ]
                if matching:
                    for entry in matching:
                        logger.warning(
                            f"  config_entry[{config_entry_domain}]: "
                            f"id={entry.get('entry_id')} "
                            f"state={entry.get('state')} "
                            f"reason={entry.get('reason')} "
                            f"source={entry.get('source')}"
                        )
                else:
                    logger.warning(
                        f"  config_entry[{config_entry_domain}]: NO entry "
                        f"visible in HA's config_entries (available: "
                        f"{entry_domains}) — install step may have written "
                        "to .storage but HA did not pick it up"
                    )
            else:
                logger.warning(
                    f"  /api/config/config_entries/entry: {len(entries)} total: "
                    f"{entry_domains}"
                )
        else:
            logger.warning(
                f"  /api/config/config_entries/entry: HTTP {entries_resp.status_code}"
            )
    except Exception as exc:  # noqa: BLE001
        # Same broad-catch rationale as the /api/services dump above.
        logger.warning(
            f"  /api/config/config_entries/entry: request failed: {type(exc).__name__}: {exc}"
        )


def _dump_docker_diagnostics(container: DockerContainer) -> None:
    """Log container status + recent docker logs for a readiness-gate failure."""
    import docker as _docker

    # docker logs --tail 100 + container state. The early ``tail=20`` grab
    # inside ``ha_container_with_fresh_config`` fires immediately after
    # container start and so does not cover the custom-component lifecycle
    # that produces the symptom.
    try:
        docker_client = _docker.from_env()
        docker_container = docker_client.containers.get(
            container.get_wrapped_container().id
        )
        logger.warning(f"  container status: {docker_container.status}")
        try:
            state_attrs = docker_container.attrs.get("State", {})
            logger.warning(
                f"  container state: exit_code={state_attrs.get('ExitCode')} "
                f"oom_killed={state_attrs.get('OOMKilled')} "
                f"restart_count={state_attrs.get('RestartCount')}"
            )
        except (KeyError, AttributeError) as exc:
            logger.warning(
                f"  container state: introspect failed: {type(exc).__name__}: {exc}"
            )
        try:
            logs = docker_container.logs(tail=100).decode("utf-8", errors="ignore")
            logger.warning(f"  docker logs --tail 100:\n{logs}")
        except _docker.errors.DockerException as exc:
            logger.warning(f"  docker logs: failed: {type(exc).__name__}: {exc}")
    except _docker.errors.DockerException as exc:
        logger.warning(f"  docker client: failed: {type(exc).__name__}: {exc}")


def _dump_ha_readiness_diagnostics(
    container: DockerContainer,
    base_url: str,
    headers: dict[str, str],
    label: str,
    *,
    config_entry_domain: str | None = None,
) -> None:
    """Emit HA-side diagnostics for any readiness-gate failure.

    Generic best-effort dump used by every readiness gate in
    ``ha_container_with_fresh_config``. The optional
    ``config_entry_domain`` argument lets the caller surface
    domain-specific presence/absence information (e.g. the sun-gate
    failure path passes ``config_entry_domain="sun"`` to make the
    config-entries section call out whether that specific entry is
    present or which state it's stuck in) instead of only the generic
    counts.

    Without it, the dump shows aggregate ``/api/services`` and
    ``/api/config/config_entries/entry`` domain lists — enough context
    to distinguish "HA never finished starting" from "HA started but a
    specific domain regressed".

    Each capture is wrapped in its own try/except so a single failure
    (container already exited, HA API gone) does not lose the other
    captures. Surfaced at WARNING level so CI logs keep the lines
    visible even with default filtering.
    """
    logger.warning(f"📋 readiness diagnostics dump ({label}):")
    _dump_services_diagnostics(base_url, headers)
    _dump_config_entries_diagnostics(base_url, headers, config_entry_domain)
    _dump_docker_diagnostics(container)
