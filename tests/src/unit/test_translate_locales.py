"""Unit coverage for ``scripts/translate_locales.py``.

The translation pipeline runs unattended in CI and commits to PR branches, so
its network-free logic is pinned here: the validation gate that decides
whether an engine answer may be written at all, the baseline-diff planning
that decides what gets retranslated or deleted, the request chunking, the
retry/backoff branching (with a monkeypatched transport — no real network),
and the catalog write-back. Only ``_call_gemini``'s happy path against the
real API is left to the live workflow run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import translate_locales  # noqa: E402
from translate_locales import (  # noqa: E402
    Plan,
    WorkItem,
    _chunk,
    _flatten,
    _plan_locale_groups,
    _plan_locale_messages,
    _plan_locale_tools,
    _unflatten,
    _validate,
)


def _item(section: str = "messages", english: str = "Hello {name}") -> WorkItem:
    return WorkItem("de", section, "greeting", english)


class TestValidate:
    def test_accepts_a_plain_translation(self) -> None:
        assert _validate(_item(english="Hello"), "Hallo") is None

    def test_rejects_empty_and_non_string(self) -> None:
        assert _validate(_item(), "") is not None
        assert _validate(_item(), "   ") is not None
        assert _validate(_item(), None) is not None
        assert _validate(_item(), 42) is not None

    def test_rejects_placeholder_drift_both_directions(self) -> None:
        assert _validate(_item(), "Hallo") is not None  # dropped {name}
        assert _validate(_item(english="Hello"), "Hallo {name}") is not None
        assert _validate(_item(), "Hallo {name}") is None

    def test_rejects_markup_outside_the_allowlist_in_messages(self) -> None:
        assert _validate(_item(english="x"), "ein <b>Wort</b>") is not None
        assert _validate(_item(english="x"), "ein <CODE>Wort</CODE>") is not None
        assert _validate(_item(english="x"), "ein <code>Wort</code>") is None

    def test_markup_rules_apply_only_to_messages(self) -> None:
        # Tool and component strings render through escapeHtml / HA core, so
        # the settings-UI markup allowlist deliberately does not gate them.
        assert _validate(_item(section="tools", english="x"), "<b>y</b>") is None

    def test_rejects_panel_link_target_drift(self) -> None:
        english = 'see the <a href="#" data-panel-link="tools">Tools</a> tab'
        ok = 'siehe <a href="#" data-panel-link="tools">Tools</a>'
        wrong = 'siehe <a href="#" data-panel-link="backups">Tools</a>'
        assert _validate(_item(english=english), ok) is None
        assert _validate(_item(english=english), wrong) is not None


class TestChunk:
    def test_splits_on_the_character_budget(self) -> None:
        big = "x" * (translate_locales._MAX_CHARS_PER_REQUEST - 10)
        batch = {"a": big, "b": "small", "c": big}
        chunks = _chunk(batch)
        assert [list(chunk) for chunk in chunks] == [["a", "b"], ["c"]]

    def test_single_item_over_budget_gets_its_own_chunk(self) -> None:
        huge = "x" * (translate_locales._MAX_CHARS_PER_REQUEST + 1)
        assert [list(c) for c in _chunk({"a": huge, "b": "y"})] == [["a"], ["b"]]

    def test_empty_batch_yields_no_chunks(self) -> None:
        assert _chunk({}) == []


class TestFlattenRoundTrip:
    def test_round_trips_nested_catalogs(self) -> None:
        nested = {"config": {"step": {"user": {"title": "Hi", "desc": "There"}}}}
        flat = _flatten(nested)
        assert flat == {
            "config.step.user.title": "Hi",
            "config.step.user.desc": "There",
        }
        assert _unflatten(flat) == nested

    def test_flatten_refuses_non_string_leaves(self) -> None:
        """A leaf the round-trip would silently delete must fail loudly
        instead — the pipeline rewrites whole files unattended in CI."""
        with pytest.raises(ValueError, match=r"a\.c"):
            _flatten({"a": {"b": "x", "c": [1, 2]}})


class TestPlanning:
    def test_missing_and_changed_messages_are_planned(self) -> None:
        plan = Plan()
        _plan_locale_messages(
            plan,
            "de",
            en_messages={"kept": "Kept", "changed": "New text", "new": "Added"},
            messages={"kept": "Behalten", "changed": "Alter Text", "orphan": "Weg"},
            changed_messages={"changed"},
        )
        assert [(i.key, i.english) for i in plan.items] == [
            ("changed", "New text"),
            ("new", "Added"),
        ]
        assert plan.deletions == [("de", "messages", "orphan")]

    def test_tools_plan_fills_changed_and_missing_fields(self) -> None:
        plan = Plan()
        _plan_locale_tools(
            plan,
            "de",
            catalog={"tools": {"ha_a": {"title": "T"}, "ha_gone": {}}},
            changed_tools={"ha_b.title"},
            tool_texts={
                "ha_a.title": "A",
                "ha_a.description": "A desc",
                "ha_b.title": "B",
                "ha_b.description": "B desc",
            },
            tool_names=frozenset({"ha_a", "ha_b"}),
        )
        assert sorted(i.key for i in plan.items) == [
            "ha_a.description",  # missing field
            "ha_b.description",  # missing entirely
            "ha_b.title",  # missing + changed
        ]
        assert plan.deletions == [("de", "tools", "ha_gone")]

    def test_groups_plan_uses_the_english_key_as_source(self) -> None:
        plan = Plan()
        _plan_locale_groups(
            plan,
            "de",
            catalog={"tool_groups": {"Old": "Alt"}},
            groups=frozenset({"Lights", "Old"}),
        )
        assert [(i.key, i.english) for i in plan.items] == [("Lights", "Lights")]
        assert plan.deletions == []

    def test_clean_tree_plans_no_work(self) -> None:
        """The no-op invariant the CI loop terminates on: after a translation
        commit repins the baseline, the retriggered run must find nothing."""
        module = translate_locales._load_test_module()
        plan = translate_locales.build_plan(module)
        assert plan.items == []
        assert plan.deletions == []


class TestCallGeminiRetry:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(translate_locales.time, "sleep", lambda _s: None)

    @staticmethod
    def _response(status_code: int, payload: dict[str, Any] | None = None) -> Any:
        return SimpleNamespace(
            status_code=status_code,
            text="body",
            json=lambda: (
                payload
                or {
                    "candidates": [
                        {"content": {"parts": [{"text": json.dumps({"s0": "Hallo"})}]}}
                    ]
                }
            ),
        )

    def test_retries_transient_statuses_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        responses = [self._response(429), self._response(503), self._response(200)]

        def fake_post(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(1)
            return responses[len(calls) - 1]

        monkeypatch.setattr(translate_locales.httpx, "post", fake_post)
        assert translate_locales._call_gemini("prompt") == {"s0": "Hallo"}
        assert len(calls) == 3

    def test_retries_transport_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        def fake_post(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 1:
                raise translate_locales.httpx.ConnectTimeout("boom")
            return self._response(200)

        monkeypatch.setattr(translate_locales.httpx, "post", fake_post)
        assert translate_locales._call_gemini("prompt") == {"s0": "Hallo"}
        assert len(calls) == 2

    def test_terminal_status_raises_without_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def fake_post(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(1)
            return self._response(400)

        monkeypatch.setattr(translate_locales.httpx, "post", fake_post)
        with pytest.raises(SystemExit, match="HTTP 400"):
            translate_locales._call_gemini("prompt")
        assert len(calls) == 1

    def test_missing_key_is_a_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GEMINI_API_KEY")
        with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
            translate_locales._call_gemini("prompt")

    def test_blocked_response_is_a_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTTP 200 with an empty candidates list (safety-filter block) must
        read like the other engine failures, not a raw IndexError."""
        blocked = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        monkeypatch.setattr(
            translate_locales.httpx,
            "post",
            lambda *_a, **_k: self._response(200, blocked),
        )
        with pytest.raises(SystemExit, match="unusable response"):
            translate_locales._call_gemini("prompt")


class TestEngineFailureDegradation:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(translate_locales.time, "sleep", lambda _s: None)
        # One string per chunk so chunk boundaries are deterministic.
        monkeypatch.setattr(translate_locales, "_MAX_CHARS_PER_REQUEST", 1)

    def test_one_failed_chunk_degrades_to_per_string_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = iter([SystemExit("quota"), {"s1": "Zwei"}])

        def fake_call(_prompt: str) -> dict[str, str]:
            answer = next(answers)
            if isinstance(answer, SystemExit):
                raise answer
            return answer

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_call)
        items = [
            WorkItem("de", "messages", "first", "One"),
            WorkItem("de", "messages", "second", "Two"),
        ]
        results, failures, dead = translate_locales._translate_locale("de", items)
        assert results == {("messages", "second"): "Zwei"}
        assert failures == ["de/messages/first: engine call failed (quota)"]
        assert dead is False

    def test_two_consecutive_failures_declare_the_engine_dead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_call(_prompt: str) -> dict[str, str]:
            raise SystemExit("quota")

        monkeypatch.setattr(translate_locales, "_call_gemini", fake_call)
        items = [
            WorkItem("de", "messages", "first", "One"),
            WorkItem("de", "messages", "second", "Two"),
            WorkItem("de", "messages", "third", "Three"),
        ]
        results, failures, dead = translate_locales._translate_locale("de", items)
        assert results == {}
        assert dead is True
        # The give-up happens after the second consecutive failure; the third
        # chunk is never attempted, so exactly two failure entries exist.
        assert len(failures) == 2


class TestMetaOnlyStub:
    def test_documented_stub_language_flow_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AGENTS.md says a new language may start as a meta-only stub catalog
        and the pipeline fills every string — so a catalog with no messages/
        tools/tool_groups sections must plan cleanly and be writable."""
        locales = tmp_path / "locales"
        locales.mkdir()
        (locales / "en.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "English", "dir": "ltr"},
                    "messages": {"greeting": "Hello"},
                    "tool_groups": {},
                    "tools": {},
                }
            ),
            encoding="utf-8",
        )
        (locales / "xx.json").write_text(
            json.dumps({"meta": {"native_name": "Testish", "dir": "ltr"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(translate_locales, "LOCALES_DIR", locales)
        monkeypatch.setattr(translate_locales, "REPO_ROOT", tmp_path)

        module = translate_locales._load_test_module()
        plan = Plan()
        translate_locales._plan_settings(plan, module, changed={})
        stub_messages = [
            item
            for item in plan.items
            if item.locale == "xx" and item.section == "messages"
        ]
        assert [item.key for item in stub_messages] == ["greeting"]

        translate_locales._apply_settings(
            "xx", translations={("messages", "greeting"): "Hallo-xx"}, deletions=[]
        )
        written = json.loads((locales / "xx.json").read_text(encoding="utf-8"))
        assert written["messages"] == {"greeting": "Hallo-xx"}


class TestApplyWrites:
    @pytest.fixture()
    def catalog_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path]:
        locales = tmp_path / "locales"
        component = tmp_path / "component"
        locales.mkdir()
        component.mkdir()
        (locales / "en.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "English", "dir": "ltr"},
                    "messages": {"first": "One", "second": "Two"},
                    "tool_groups": {},
                    "tools": {},
                }
            ),
            encoding="utf-8",
        )
        (locales / "de.json").write_text(
            json.dumps(
                {
                    "meta": {"native_name": "Deutsch", "dir": "ltr"},
                    # Deliberately not in en.json order; orphan must go.
                    "messages": {"second": "Zwei", "orphan": "Weg"},
                    "tool_groups": {},
                    "tools": {},
                }
            ),
            encoding="utf-8",
        )
        (component / "en.json").write_text(
            json.dumps({"config": {"title": "Hi"}}), encoding="utf-8"
        )
        (component / "de.json").write_text(
            json.dumps({"config": {"title": "Alt"}}), encoding="utf-8"
        )
        monkeypatch.setattr(translate_locales, "LOCALES_DIR", locales)
        monkeypatch.setattr(translate_locales, "COMPONENT_DIR", component)
        # The "updated <path>" print renders paths repo-relative.
        monkeypatch.setattr(translate_locales, "REPO_ROOT", tmp_path)
        return locales, component

    def test_settings_writes_mirror_en_order_and_drop_orphans(
        self, catalog_dirs: tuple[Path, Path]
    ) -> None:
        locales, _component = catalog_dirs
        translate_locales._apply_settings(
            "de",
            translations={("messages", "first"): "Eins"},
            deletions=[("de", "messages", "orphan")],
        )
        written = json.loads((locales / "de.json").read_text(encoding="utf-8"))
        assert list(written["messages"]) == ["first", "second"]
        assert written["messages"]["first"] == "Eins"
        assert "orphan" not in written["messages"]

    def test_component_write_round_trips_nested_shape(
        self, catalog_dirs: tuple[Path, Path]
    ) -> None:
        _locales, component = catalog_dirs
        translate_locales._apply_component(
            "de", translations={("component", "config.title"): "Hallo"}, deletions=[]
        )
        written = json.loads((component / "de.json").read_text(encoding="utf-8"))
        assert written == {"config": {"title": "Hallo"}}

    def test_apply_is_idempotent(self, catalog_dirs: tuple[Path, Path]) -> None:
        """A second no-work pass must not rewrite the file — this is the
        property that keeps the CI auto-commit loop from ping-ponging."""
        locales, _component = catalog_dirs
        translate_locales._apply_settings(
            "de", translations={}, deletions=[("de", "messages", "orphan")]
        )
        settled = (locales / "de.json").read_text(encoding="utf-8")
        translate_locales._apply_settings("de", translations={}, deletions=[])
        assert (locales / "de.json").read_text(encoding="utf-8") == settled
