"""Unit tests for the HACS GitHub-token injection helper.

``inject_hacs_token`` is the shared pure transform behind both CI
injection paths (the testcontainer seed copy in ``e2e/conftest.py`` and
the HAOS qcow2 pre-boot edit in ``haos_runtime.inject_hacs_token_in_qcow2``).
It authenticates HACS in CI so repository adds don't ride the shared-IP
60 req/h unauthenticated GitHub budget — the long-standing HACS-install
e2e flake ("GitHub Ratelimit error" in the HA core log).

``haos_runtime`` is loaded by file path: its module-level imports are
stdlib-only, so this collects everywhere, without assuming ``tests/src``
is importable as a package.
"""

import importlib.util
import sys
from pathlib import Path

HAOS_RUNTIME_PATH = Path(__file__).resolve().parents[1] / "haos_runtime.py"


def _load_haos_runtime():
    spec = importlib.util.spec_from_file_location("haos_runtime", HAOS_RUNTIME_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


haos_runtime = _load_haos_runtime()


def _doc(entries):
    return {"version": 1, "data": {"entries": entries}}


def test_patches_hacs_entry_and_returns_true():
    doc = _doc(
        [
            {"domain": "sun", "data": {}},
            {"domain": "hacs", "data": {}, "options": {"experimental": False}},
        ]
    )
    assert haos_runtime.inject_hacs_token(doc, "ghs_test_token") is True
    hacs = doc["data"]["entries"][1]
    # Same shape HACS' own config flow persists after device-flow auth.
    assert hacs["data"] == {"token": "ghs_test_token"}
    # Only ``data`` is replaced — the rest of the entry is untouched.
    assert hacs["options"] == {"experimental": False}


def test_replaces_existing_stale_token():
    doc = _doc([{"domain": "hacs", "data": {"token": "stale"}}])
    assert haos_runtime.inject_hacs_token(doc, "fresh") is True
    assert doc["data"]["entries"][0]["data"] == {"token": "fresh"}


def test_no_hacs_entry_returns_false_and_leaves_doc_untouched():
    entries = [{"domain": "sun", "data": {}}, {"domain": "demo", "data": {"a": 1}}]
    doc = _doc([dict(e) for e in entries])
    assert haos_runtime.inject_hacs_token(doc, "tok") is False
    assert doc == _doc(entries)


def test_other_entries_untouched_when_hacs_patched():
    doc = _doc(
        [
            {"domain": "demo", "data": {"keep": True}},
            {"domain": "hacs", "data": {}},
        ]
    )
    haos_runtime.inject_hacs_token(doc, "tok")
    assert doc["data"]["entries"][0] == {"domain": "demo", "data": {"keep": True}}


def test_qcow2_injector_is_noop_without_token(monkeypatch, tmp_path):
    """No GITHUB_TOKEN -> no guestfish invocation, no exception."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def _fail(*args, **kwargs):
        raise AssertionError("guestfish must not run without GITHUB_TOKEN")

    monkeypatch.setattr(haos_runtime.subprocess, "run", _fail)
    haos_runtime.inject_hacs_token_in_qcow2(tmp_path / "image.qcow2")
