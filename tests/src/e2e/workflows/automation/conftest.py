"""Pytest hooks for surfacing #1389 ``_POLL_CADENCE`` measurement to CI logs.

``logger.info`` from inside a test method is captured by pytest-xdist's
per-worker buffer and only surfaces on test FAILURE; PASSED tests' INFO
output is silently dropped. Mirrors the ``Readiness gate timings`` pattern
in ``tests/src/e2e/conftest.py``:

1. Worker-side ``_POLL_CADENCE_MEASUREMENTS`` collects per-test percentile
   dicts.
2. ``pytest_sessionfinish`` (worker hook) pushes the list into
   ``session.config.workeroutput`` — the xdist channel that reaches the
   master process.
3. ``pytest_testnodedown`` (master hook) reads each finished worker's
   output and aggregates into ``_ALL_POLL_CADENCE_MEASUREMENTS``.
4. ``pytest_terminal_summary`` (master hook) renders a terminal section
   via ``terminalreporter.write_line`` — writes outside the per-worker
   capture buffer, so the measurement table lands in the visible CI log.

The fallback to the local list (when running without xdist) preserves
the ad-hoc local execution path.
"""

from typing import Any

# Worker-side storage. Tests call ``record_poll_cadence_measurement`` to
# add a percentile dict; ``pytest_sessionfinish`` ships the list up.
_POLL_CADENCE_MEASUREMENTS: list[dict[str, Any]] = []

# Master-side aggregate. ``pytest_testnodedown`` fills this from each
# worker's ``workeroutput``; ``pytest_terminal_summary`` renders it.
_ALL_POLL_CADENCE_MEASUREMENTS: list[dict[str, Any]] = []


def record_poll_cadence_measurement(measurement: dict[str, Any]) -> None:
    """Record a single #1389 measurement run from a test method.

    Expected keys: ``n``, ``p50``, ``p90``, ``p99``, ``min``, ``max``,
    ``not_verified``, ``verdict``, ``samples``. Floats are rendered with
    one-decimal precision in the terminal section.
    """
    _POLL_CADENCE_MEASUREMENTS.append(measurement)


def pytest_sessionfinish(session, exitstatus):
    """xdist worker hook: hand collected measurements up to the master.

    ``config.workeroutput`` only exists on workers; on the master (or
    when running without xdist) the attribute is missing, and the local
    list is read directly by ``pytest_terminal_summary`` instead.
    """
    del exitstatus
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None and _POLL_CADENCE_MEASUREMENTS:
        workeroutput["poll_cadence_measurements"] = list(_POLL_CADENCE_MEASUREMENTS)


def pytest_testnodedown(node, error):
    """xdist master hook: collect a finished worker's measurements."""
    del error
    samples = getattr(node, "workeroutput", {}).get("poll_cadence_measurements", [])
    _ALL_POLL_CADENCE_MEASUREMENTS.extend(samples)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Master-side hook: render the measurement table outside xdist capture.

    Falls back to the local list when running without xdist (no
    ``pytest_testnodedown`` fires in that mode, so ``_ALL_*`` stays empty).
    """
    del exitstatus, config
    measurements = _ALL_POLL_CADENCE_MEASUREMENTS or _POLL_CADENCE_MEASUREMENTS
    if not measurements:
        return
    terminalreporter.section("#1389 _POLL_CADENCE measurement")
    for m in measurements:
        terminalreporter.write_line(
            f"[POLL_CADENCE_1389] N={m['n']}/{m.get('attempts', m['n'])} "
            f"p50={m['p50']:.1f}ms p90={m['p90']:.1f}ms p99={m['p99']:.1f}ms "
            f"min={m['min']:.1f}ms max={m['max']:.1f}ms "
            f"not_verified={m['not_verified']} VERDICT={m['verdict']}"
        )
        terminalreporter.write_line(f"[POLL_CADENCE_1389] samples_ms={m['samples']}")
