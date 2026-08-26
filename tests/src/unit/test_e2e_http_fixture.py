"""Guard the e2e HTTP fixture against HA 2026.8's self-reverting trial.

Regression cover for the failure that took out the external HAOS lane
(#2146's PR): HA 2026.8 moved HTTP config into a storage-backed store and
turned a ``http:`` YAML block into a ONE-SHOT migration that lands as an
unconfirmed *pending trial* — Core serves it, schedules a revert five
minutes later, and when nothing promotes it, reverts to the stored config
and RESTARTS Core.

That is the worst kind of failure to diagnose: the symptom is a scatter of
unrelated failures in the suite's tail (``Server disconnected without
sending a response``) rather than a named error, and it only appears in
lanes whose run outlives the five-minute timer. So the fixture is pinned
here instead of being left to a future reader's memory:

* ``configuration.yaml`` must carry NO ``http:`` block — its presence is
  what stages the trial.
* ``.storage/http`` must be a v2 store whose config is already CONFIRMED
  (``pending: null``, ``yaml_migration_done: true``), so no trial is ever
  staged and no revert fires.
* ``server_port`` must stay 8123: ``tests/src/haos_runtime.py`` forwards
  only guest 8123, so changing it hangs every HAOS lane for the full
  180s port wait and then fails with an opaque TimeoutError.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _REPO_ROOT / "tests" / "initial_test_state"
_CONFIG_YAML = _FIXTURE / "configuration.yaml"
_HTTP_STORE = _FIXTURE / ".storage" / "http"


class _TagTolerantLoader(yaml.SafeLoader):
    """SafeLoader that tolerates HA's ``!include``-style tags.

    Still safe: it subclasses SafeLoader (no ``!!python/...`` construction)
    and its only addition maps every ``!``-tagged node to ``None`` rather
    than instantiating anything. The fixture's tags point at sibling files
    this test does not care about.
    """


_TagTolerantLoader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)


@pytest.fixture(scope="module")
def config_yaml() -> dict:
    return yaml.load(_CONFIG_YAML.read_text(encoding="utf-8"), _TagTolerantLoader)


@pytest.fixture(scope="module")
def http_store() -> dict:
    return json.loads(_HTTP_STORE.read_text(encoding="utf-8"))


def test_configuration_yaml_declares_no_http_block(config_yaml):
    assert "http" not in config_yaml, (
        "an http: block in the e2e configuration.yaml makes HA 2026.8 stage "
        "an unconfirmed pending trial and RESTART Core about five minutes "
        "in, scattering failures through the suite's tail. Put HTTP settings "
        "in tests/initial_test_state/.storage/http instead."
    )


def test_http_store_is_a_confirmed_v2_config(http_store):
    assert http_store["version"] == 2, "HA 2026.8 reads a v2 http store"
    assert http_store["key"] == "http"
    data = http_store["data"]
    assert data["yaml_migration_done"] is True, (
        "with the migration unfinished HA would migrate a YAML block (or "
        "synthesize a default) and stage it as a trial"
    )
    assert data["pending"] is None, (
        "a pending config IS the trial: HA would serve it, schedule a revert "
        "five minutes out, and restart Core when nothing promotes it"
    )


def test_http_store_pins_port_8123(http_store):
    assert http_store["data"]["stable"]["server_port"] == 8123, (
        "the HAOS runtime harness forwards only guest 8123; another port "
        "hangs every HAOS lane for the full port wait and fails opaquely. "
        "(HA 2026.8 defaults Supervisor installs to port 80, which is why "
        "this has to be pinned rather than left to the default.)"
    )


def test_http_store_keeps_the_proxy_settings_the_yaml_block_carried(http_store):
    stable = http_store["data"]["stable"]
    assert stable["use_x_forwarded_for"] is True
    assert stable["trusted_proxies"], (
        "the proxy pair moved here from configuration.yaml; dropping it "
        "silently changes what the suite exercises"
    )
