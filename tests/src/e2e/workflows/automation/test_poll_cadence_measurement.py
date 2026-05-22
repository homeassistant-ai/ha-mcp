"""Measurement test for issue #1389 — _POLL_CADENCE p50/worst validation.

This test creates ``N=10`` automations cold-start, captures the
``entity-registration-elapsed`` DEBUG log line emitted by
``rest_client._poll_for_automation_entity`` (introduced in this PR), and
emits a percentile summary at INFO level so the numbers surface in CI
output (default ``log_cli_level=INFO``).

The #1389 decision rule is applied to the reported numbers via PR
comment — the test itself is report-only and does not gate CI on the
threshold (CI latency variance from parallel pytest-xdist workers
sharing one HA testcontainer would make a strict threshold-assert
flaky). The "VERDICT" line in the output makes the result machine-greppable.

The headline metric is ``worst`` (highest observed sample) rather than
a true statistical p99: at N=10 the 99th-percentile index collapses to
the same bucket as p90, so the field would silently drop the
outlier-detection role it was named for. ``worst`` reflects what the
threshold-check actually measures.
"""

import logging
import re
import statistics

import pytest

from ha_mcp.tools.tools_config_automations import NOT_VERIFIED_WARNING_PREFIX

from ...conftest import record_poll_cadence_measurement
from ...utilities.assertions import safe_call_tool
from ...utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

logger = logging.getLogger(__name__)

# Captures the DEBUG line emitted by rest_client._poll_for_automation_entity
# on every successful registration. Format owned by that function.
_ELAPSED_RE = re.compile(r"entity-registration-elapsed:\s*([\d.]+)ms")


@pytest.mark.automation
@pytest.mark.cleanup
@pytest.mark.external_only
class TestPollCadenceMeasurement1389:
    """Empirical p50/p99 measurement of automation entity-registration latency.

    Uses ``caplog`` to read the DEBUG records emitted by
    ``_poll_for_automation_entity``. That capture only works when the
    rest_client runs in the same process as pytest (testcontainer +
    HAOS-external modes); on the HAOS-inaddon tier the rest_client lives
    in the addon's separate process and its log records never reach
    ``caplog.records``. Marked ``external_only`` so the test is skipped
    on the inaddon tier instead of failing with a false-negative
    "no records captured" assertion.
    """

    N_SAMPLES = 10

    async def test_poll_cadence_p50_worst(
        self, mcp_client, cleanup_tracker, test_data_factory, caplog
    ):
        # ``caplog.at_level`` saves+restores the logger level on exit;
        # ``caplog.set_level`` would leave DEBUG-on-rest_client sticky
        # for every later test on the same xdist worker. Wraps only the
        # create-loop so unrelated DEBUG noise from teardown stays out.
        created_entity_ids: list[str] = []
        try:
            with caplog.at_level(logging.DEBUG, logger="ha_mcp.client.rest_client"):
                caplog.clear()
                not_verified_count = 0
                for i in range(self.N_SAMPLES):
                    config = test_data_factory.automation_config(
                        f"Poll Cadence Measurement 1389-{i:02d}",
                    )
                    create_data = await safe_call_tool(
                        mcp_client, "ha_config_set_automation", {"config": config}
                    )
                    assert create_data.get("success"), (
                        f"automation creation #{i} failed: {create_data}"
                    )
                    entity_id = create_data.get("entity_id")
                    if (
                        entity_id
                        and isinstance(entity_id, str)
                        and entity_id.startswith("automation.")
                    ):
                        cleanup_tracker.track("automation", entity_id)
                        created_entity_ids.append(entity_id)
                    # ``ha_config_set_automation`` pops ``entity_not_verified``
                    # off the response before returning and translates the
                    # miss into a ``warnings`` entry — scan that instead.
                    # Missing this signal would let a "RETUNE NEEDED" run
                    # silently print VERDICT=VALIDATED.
                    warnings = create_data.get("warnings") or []
                    if any(
                        isinstance(w, str)
                        and w.startswith(NOT_VERIFIED_WARNING_PREFIX)
                        for w in warnings
                    ):
                        not_verified_count += 1

            # Parse elapsed-ms values out of captured DEBUG records.
            samples: list[float] = []
            for record in caplog.records:
                if record.name != "ha_mcp.client.rest_client":
                    continue
                match = _ELAPSED_RE.search(record.getMessage())
                if match:
                    samples.append(float(match.group(1)))

            assert samples, (
                f"No 'entity-registration-elapsed' DEBUG records captured after "
                f"{self.N_SAMPLES} automation creations. Either the instrumentation "
                f"is missing, the logger name changed, or the format regex drifted."
            )

            # Sample-count invariant pairs with the miss detection above:
            # every successful iteration must contribute exactly one elapsed
            # record. A partial capture (9 samples + 1 miss not detected by
            # the warning scan) would silently skew percentiles otherwise.
            assert len(samples) + not_verified_count == self.N_SAMPLES, (
                f"sample-count invariant violated: {len(samples)} samples + "
                f"{not_verified_count} not-verified != {self.N_SAMPLES} attempts"
            )

            samples.sort()
            n = len(samples)
            p50 = statistics.median(samples)
            p90 = samples[max(0, int(0.9 * n) - 1)]
            # At N=10, ``int(0.99 * (n-1))`` collapses to the same index as
            # p90 — a real statistical p99 needs more samples. Use the
            # highest observed sample under the name ``worst`` so the
            # decision-rule threshold reflects what it actually checks.
            worst = samples[-1]
            s_min = samples[0]
            s_max = samples[-1]

            p50_ok = p50 < 100.0
            worst_ok = worst < 1000.0
            no_misses = not_verified_count == 0
            verdict = (
                "VALIDATED" if (p50_ok and worst_ok and no_misses) else "RETUNE NEEDED"
            )

            # ``logger.info`` from a test method is captured by pytest-xdist's
            # per-worker buffer and only surfaces on FAILURE — PASSED tests
            # silently drop the INFO output. Route through the conftest
            # recorder so ``pytest_terminal_summary`` renders the table on
            # the master, outside the capture buffer.
            record_poll_cadence_measurement(
                {
                    "n": n,
                    "attempts": self.N_SAMPLES,
                    "p50": p50,
                    "p90": p90,
                    "worst": worst,
                    "min": s_min,
                    "max": s_max,
                    "not_verified": not_verified_count,
                    "verdict": verdict,
                    "samples": [round(s, 1) for s in samples],
                }
            )

            sep = "=" * 70
            logger.info(sep)
            logger.info(
                "#1389 _POLL_CADENCE measurement (N=%d successful samples / %d attempts, "
                "%d not-verified)",
                n,
                self.N_SAMPLES,
                not_verified_count,
            )
            logger.info(sep)
            logger.info(
                "p50   = %6.1f ms  (rule: < 100ms  → %s)",
                p50,
                "PASS" if p50_ok else "RETUNE",
            )
            logger.info("p90   = %6.1f ms", p90)
            logger.info(
                "worst = %6.1f ms  (rule: < 1000ms → %s)",
                worst,
                "PASS" if worst_ok else "RETUNE",
            )
            logger.info("min   = %6.1f ms  / max = %6.1f ms", s_min, s_max)
            logger.info("samples (sorted, ms): %s", [round(s, 1) for s in samples])
            logger.info("VERDICT: %s", verdict)
            logger.info(sep)
        finally:
            # The logging-only ``cleanup_tracker`` fixture only logs what
            # it tracked; without an explicit delete here this test would
            # leak 10 automations into the next worker run on a loadscope
            # split. Best-effort: per-entity remove, swallow only transient
            # transport/HA errors so programmer bugs (TypeError, KeyError,
            # AttributeError, AssertionError) propagate with their stack
            # trace instead of being downgraded to a warning line.
            for ent_id in created_entity_ids:
                try:
                    await safe_call_tool(
                        mcp_client,
                        "ha_config_remove_automation",
                        {"identifier": ent_id},
                    )
                except _POLLING_TRANSIENT_ERRORS as cleanup_err:
                    logger.warning(
                        "cleanup: failed to remove %s: %s", ent_id, cleanup_err
                    )
