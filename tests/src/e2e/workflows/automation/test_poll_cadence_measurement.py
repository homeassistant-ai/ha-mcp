"""Measurement test for issue #1389 — _POLL_CADENCE p50/p99 validation.

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

Decision rule (per #1389):
- ``p50 < 100 ms`` AND ``p99 < 1.0 s`` → cadence validated, close #1389
- otherwise → open follow-up retune PR with the measurement table
"""

import logging
import re
import statistics

import pytest

from ...utilities.assertions import safe_call_tool

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

    async def test_poll_cadence_p50_p99(
        self, mcp_client, cleanup_tracker, test_data_factory, caplog
    ):
        # Capture the DEBUG records emitted by ``_poll_for_automation_entity``
        # during this test only. ``caplog`` is per-test-method-scoped, so
        # records emitted by other tests in the same worker (before or
        # after this one) don't appear in ``caplog.records`` here. The
        # rest_client logger-name filter + regex below narrow to the
        # elapsed-ms records specifically, and ``caplog.clear()`` at entry
        # is belt-and-suspenders for any setup-phase noise.
        caplog.set_level(logging.DEBUG, logger="ha_mcp.client.rest_client")

        # Clear any pre-existing matching records so a long-running worker's
        # earlier automation creates don't leak into our sample set.
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
            # Track misses — when _poll_for_automation_entity returns None
            # after exhausting _POLL_CADENCE, the create_data carries this
            # flag and no elapsed-ms record is emitted for that iteration.
            # Such misses are themselves evidence that the cadence is too
            # short (p99 >= sum(_POLL_CADENCE) = 6.0s), so a high miss
            # rate flips the decision toward RETUNE.
            if create_data.get("entity_not_verified"):
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

        samples.sort()
        n = len(samples)
        p50 = statistics.median(samples)
        p90 = samples[max(0, int(0.9 * n) - 1)]
        # For small ``N_SAMPLES`` (=10), ``int(0.99 * (n-1))`` collapses to
        # the same index as p90 (both → 8 for n=10), silently dropping the
        # worst-case sample. Use the maximum as p99 so an outlier can't
        # violate the decision-rule threshold without surfacing.
        p99 = samples[-1]
        s_min = samples[0]
        s_max = samples[-1]

        # Decision rule per #1389 body. Any not-verified iteration is itself
        # a p99 ≥ 6.0s signal (cadence exhausted without match) and flips to
        # RETUNE regardless of the measured samples.
        p50_ok = p50 < 100.0
        p99_ok = p99 < 1000.0
        no_misses = not_verified_count == 0
        verdict = "VALIDATED" if (p50_ok and p99_ok and no_misses) else "RETUNE NEEDED"

        # INFO surfaces in CI logs (default log_cli_level=INFO).
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
            "p50 = %6.1f ms  (rule: < 100ms  → %s)",
            p50,
            "PASS" if p50_ok else "RETUNE",
        )
        logger.info("p90 = %6.1f ms", p90)
        logger.info(
            "p99 = %6.1f ms  (rule: < 1000ms → %s)",
            p99,
            "PASS" if p99_ok else "RETUNE",
        )
        logger.info("min = %6.1f ms  / max = %6.1f ms", s_min, s_max)
        logger.info("samples (sorted, ms): %s", [round(s, 1) for s in samples])
        logger.info("VERDICT: %s", verdict)
        logger.info(sep)
