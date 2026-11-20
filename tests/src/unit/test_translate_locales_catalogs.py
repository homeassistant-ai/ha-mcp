"""What ``scripts/translate_locales.py`` plans, writes, records and repins.

The pipeline runs unattended in CI and commits to PR branches. Each test runs
its command line on a scratch repository: ``--dry-run`` for the plan (what is
retranslated, filled or deleted), a full run for the catalogs it writes, the
progress record a partial run leaves for the next one, and the baseline a
finished run repins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from ._locale_sync_repo import (
    COMPONENT_SURFACE,
    SETTINGS_SURFACE,
    TOOLS_SURFACE,
    LocaleRepo,
    Request,
    Run,
    answer,
)


@pytest.fixture
def repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> LocaleRepo:
    return LocaleRepo(tmp_path, monkeypatch, capsys)


def _planned(run: Run) -> set[str]:
    """The ``locale/section/key`` strings a dry run plans to translate."""
    assert run.code == 0, run
    return {
        line.strip()
        for line in run.out.splitlines()[1:]
        if line.startswith("  ") and not line.startswith("  delete ")
    }


def _deleted(run: Run) -> set[str]:
    return {
        line.removeprefix("  delete ")
        for line in run.out.splitlines()
        if line.startswith("  delete ")
    }


def _answers(repo: LocaleRepo, translations: dict[str, str]) -> None:
    """The engine answers these identifiers so, and any other with its English."""

    def reply(request: Request) -> httpx.Response:
        return answer(
            {sid: translations.get(sid, text) for sid, text in request.strings.items()}
        )

    repo.engine.reply = reply


def _tools(*tools: tuple[str, str, str, str]) -> str:
    """Tool source for ``(name, tag, title, docstring)`` tools."""
    return "".join(
        f'\n@mcp.tool(tags={{"{tag}"}}, annotations={{"title": "{title}"}})\n'
        f'async def {name}() -> None:\n    """{doc}"""\n'
        for name, tag, title, doc in tools
    )


class TestPlanning:
    def test_best_effort_settings_locale_is_not_an_automatic_target(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_settings(
            "en", {"greeting": "Hello"}, native_name="English", tools=False
        )
        repo.write_settings("tlh", {}, native_name="tlhIngan Hol")
        assert _planned(repo.run("--dry-run")) == {"de/messages/greeting"}

    def test_best_effort_component_locale_is_not_planned(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_component("en", {"config": {"title": "English"}})
        repo.write_component("tlh", {})
        assert _planned(repo.run("--dry-run")) == {"de/component/config.title"}

    def test_missing_and_changed_messages_are_planned(self, repo: LocaleRepo) -> None:
        repo.write_settings(
            "en",
            {"kept": "Kept", "changed": "New text", "new": "Added"},
            native_name="English",
            tools=False,
        )
        repo.write_settings(
            "de", {"kept": "Behalten", "changed": "Alter Text", "orphan": "Weg"}
        )
        repo.stale(SETTINGS_SURFACE, "messages.changed")
        run = repo.run("--dry-run")
        assert _planned(run) == {"de/messages/changed", "de/messages/new"}
        assert _deleted(run) == {"de/messages/orphan"}

    def test_tools_plan_fills_changed_and_missing_fields(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_tools(
            _tools(("ha_a", "Sample", "A", "A desc"), ("ha_b", "Sample", "B", "B desc"))
        )
        repo.write_json(
            repo.settings_dir / "de.json",
            {
                "meta": {"native_name": "Deutsch"},
                "messages": {},
                "tools": {"ha_a": {"title": "T"}, "ha_gone": {}},
                "tool_groups": {"Sample": "Beispiel"},
            },
        )
        repo.stale(TOOLS_SURFACE, "ha_b.title")
        run = repo.run("--dry-run")
        assert _planned(run) == {
            "de/tools/ha_a.description",  # missing field
            "de/tools/ha_b.description",  # missing entirely
            "de/tools/ha_b.title",  # missing + changed
        }
        assert _deleted(run) == {"de/tools/ha_gone"}

    def test_groups_plan_uses_the_english_key_as_source(self, repo: LocaleRepo) -> None:
        repo.write_tools(
            _tools(("ha_a", "Lights", "A", "A desc"), ("ha_b", "Old", "B", "B desc"))
        )
        tools = {name: {"title": "T", "description": "D"} for name in ("ha_a", "ha_b")}
        repo.write_json(
            repo.settings_dir / "de.json",
            {
                "meta": {"native_name": "Deutsch"},
                "messages": {},
                "tools": tools,
                "tool_groups": {"Old": "Alt"},
            },
        )
        run = repo.run()
        assert run.code == 0, run
        assert [request.strings for request in repo.engine.requests] == [
            {"tool_groups:Lights": "Lights"}
        ]
        assert repo.settings("de")["tool_groups"] == {"Lights": "Lights", "Old": "Alt"}


class TestFlattenRoundTrip:
    def test_round_trips_nested_catalogs(self, repo: LocaleRepo) -> None:
        nested = {"config": {"step": {"user": {"title": "Hi", "desc": "There"}}}}
        repo.write_component("en", nested)
        assert repo.run().code == 0
        assert repo.component("de") == nested

    def test_flatten_refuses_non_string_leaves(self, repo: LocaleRepo) -> None:
        """A leaf the round-trip would silently delete must fail loudly
        instead — the pipeline rewrites whole files unattended in CI."""
        repo.write_component("en", {"a": {"b": "x", "c": "y"}})
        catalog = {"a": {"b": "x", "c": [1, 2]}}
        repo.write_component("de", catalog)
        with pytest.raises(ValueError, match=r"a\.c"):
            repo.run()
        assert repo.component("de") == catalog


class TestResumableProgress:
    """A run that fails part of its work records which locales already got
    each changed string, so the rerun does not spend quota on them again."""

    @pytest.fixture(autouse=True)
    def _changed_greeting(self, repo: LocaleRepo) -> None:
        repo.write_settings(
            "en", {"greeting": "Hello"}, native_name="English", tools=False
        )
        for locale in ("de", "es", "ru"):
            repo.write_settings(locale, {"greeting": "alt"}, native_name=locale)
        repo.stale(SETTINGS_SURFACE, "messages.greeting")

    @staticmethod
    def _failing(repo: LocaleRepo, *locales: str) -> None:
        """The engine refuses every request for ``locales``."""

        def reply(request: Request) -> httpx.Response:
            if any(f"({locale})" in request.prompt for locale in locales):
                return httpx.Response(400)
            return answer(request.strings)

        repo.engine.reply = reply

    def test_partial_run_records_and_next_plan_skips(self, repo: LocaleRepo) -> None:
        self._failing(repo, "es", "ru")
        assert repo.run().code == 2
        assert _planned(repo.run("--dry-run")) == {
            "es/messages/greeting",
            "ru/messages/greeting",
        }
        # A further English change invalidates what de received too.
        repo.write_settings(
            "en", {"greeting": "Hello again"}, native_name="English", tools=False
        )
        assert _planned(repo.run("--dry-run")) == {
            "de/messages/greeting",
            "es/messages/greeting",
            "ru/messages/greeting",
        }

    def test_record_merges_and_full_success_clears_it(self, repo: LocaleRepo) -> None:
        self._failing(repo, "es", "ru")
        assert repo.run().code == 2
        self._failing(repo, "ru")
        assert repo.run().code == 2
        assert _planned(repo.run("--dry-run")) == {"ru/messages/greeting"}

        self._failing(repo)
        assert repo.run().code == 0
        assert not repo.progress_path.exists()
        assert _planned(repo.run("--dry-run")) == set()


class TestRepinBaseline:
    """A feature-gated tool has two English renderings: the hand-written stub
    the catalogs translate, and the parsed docstring the baseline pins under
    a suffixed key."""

    _GATED = "ha_config_get_yaml"
    _PARSED = f"{_GATED}.description (parsed)"

    @pytest.fixture(autouse=True)
    def _gated_tool(self, repo: LocaleRepo) -> None:
        repo.write_tools(_tools((self._GATED, "System", "Raw YAML", "Read raw YAML.")))
        repo.write_json(
            repo.settings_dir / "de.json",
            {
                "meta": {"native_name": "Deutsch"},
                "messages": {},
                "tools": {self._GATED: {"title": "T", "description": "D"}},
                "tool_groups": {"System": "System"},
            },
        )

    def test_changed_parsed_keys_stay_stale_for_human_review(
        self, repo: LocaleRepo
    ) -> None:
        """A moved parsed docstring on a feature-gated tool keeps its OLD
        baseline hash — the red staleness test is the human checkpoint that
        the hand-written stub still describes the tool; only the manual
        update_locale_baseline.py repin (the confirmation) clears it. It is
        no translation work either: nothing on screen went stale."""
        rendered = f"{self._GATED}.description"
        repo.write_baseline(
            {TOOLS_SURFACE: {self._PARSED: "oldhash", rendered: "oldrendered"}}
        )
        run = repo.run()
        assert run.code == 0, run
        assert [list(request.strings) for request in repo.engine.requests] == [
            [f"tools:{rendered}"]
        ]
        written = repo.read_json(repo.baseline_path)[TOOLS_SURFACE]
        assert written[self._PARSED] == "oldhash"
        assert written[rendered] != "oldrendered"
        assert (
            f"held stale (needs human stub review + manual repin): {self._PARSED}"
            in (run.err)
        )

    def test_new_parsed_keys_pin_fresh(self, repo: LocaleRepo) -> None:
        run = repo.run()
        assert run.code == 0, run
        assert self._PARSED in repo.read_json(repo.baseline_path)[TOOLS_SURFACE]


def test_documented_stub_language_flow_does_not_crash(repo: LocaleRepo) -> None:
    """A new language starts as a near-empty catalog the pipeline fills, and
    the sections it does not carry yet are absent rather than empty — so a
    catalog with no messages/tools/tool_groups sections must plan cleanly
    and be writable. What a shipped catalog owes beyond that is checked
    against the real files (test_translate_locales_shipped_samples.py), not
    here."""
    repo.write_settings("en", {"greeting": "Hello"}, native_name="English", tools=False)
    repo.write_json(
        repo.settings_dir / "xx.json",
        {"meta": {"native_name": "Testish", "dir": "ltr"}},
    )
    _answers(repo, {"messages:greeting": "Hallo-xx"})
    run = repo.run()
    assert run.code == 0, run
    assert repo.settings("xx")["messages"] == {"greeting": "Hallo-xx"}


class TestSharedTranslationReuse:
    """A string both authored surfaces carry reads the same on both."""

    @pytest.fixture(autouse=True)
    def _shared(self, repo: LocaleRepo) -> None:
        repo.write_settings(
            "en", {"shared": "Unknown"}, native_name="English", tools=False
        )
        repo.write_component("en", {"common": {"version_unknown": "Unknown"}})
        repo.write_component("de", {"common": {"version_unknown": "unbekannt"}})

    def test_missing_shared_key_reuses_existing_sibling_before_engine_call(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_settings(
            "en",
            {"shared": "Unknown", "other": "Other"},
            native_name="English",
            tools=False,
        )
        _answers(repo, {"messages:other": "Andere"})
        assert repo.run().code == 0
        assert [list(request.strings) for request in repo.engine.requests] == [
            ["messages:other"]
        ]
        assert repo.settings("de")["messages"] == {
            "shared": "unbekannt",
            "other": "Andere",
        }

    def test_english_sibling_is_translated_instead_of_reused(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_component("de", {"common": {"version_unknown": "Unknown"}})
        _answers(repo, {"messages:shared": "unbekannt"})
        assert repo.run().code == 0
        assert len(repo.engine.requests) == 1
        assert "messages:shared" in repo.engine.requests[0].strings
        assert repo.settings("de")["messages"]["shared"] == "unbekannt"
        assert repo.component("de")["common"]["version_unknown"] == "unbekannt"

    def test_english_settings_sibling_is_translated_instead_of_reused(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_settings("de", {"shared": "Unknown"})
        repo.write_component("de", {"common": {}})
        _answers(repo, {"messages:shared": "unbekannt"})
        assert repo.run().code == 0
        assert [list(request.strings) for request in repo.engine.requests] == [
            ["messages:shared"]
        ]
        assert repo.settings("de")["messages"]["shared"] == "unbekannt"
        assert repo.component("de")["common"]["version_unknown"] == "unbekannt"

    def test_disagreeing_siblings_are_translated_instead_of_reused(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_component(
            "en", {"common": {"version_unknown": "Unknown", "other_unknown": "Unknown"}}
        )
        repo.write_component(
            "de",
            {
                "common": {
                    "version_unknown": "unbekannt",
                    "other_unknown": "nicht bekannt",
                }
            },
        )
        _answers(repo, {"messages:shared": "unbekannt"})
        assert repo.run().code == 0
        assert len(repo.engine.requests) == 1
        assert "messages:shared" in repo.engine.requests[0].strings
        assert repo.settings("de")["messages"]["shared"] == "unbekannt"
        assert repo.component("de")["common"] == {
            "version_unknown": "unbekannt",
            "other_unknown": "unbekannt",
        }

    def test_sibling_invalid_for_destination_is_translated_instead_of_reused(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_component(
            "de", {"common": {"version_unknown": "<script>unbekannt</script>"}}
        )
        _answers(repo, {"messages:shared": "unbekannt"})
        assert repo.run().code == 0
        assert len(repo.engine.requests) == 1
        assert "messages:shared" in repo.engine.requests[0].strings
        assert repo.settings("de")["messages"]["shared"] == "unbekannt"
        assert repo.component("de")["common"]["version_unknown"] == "unbekannt"

    def test_component_partial_result_invalid_for_settings_is_not_aligned(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_settings("de", {"shared": "alt"})
        repo.stale(SETTINGS_SURFACE, "messages.shared")
        repo.stale(COMPONENT_SURFACE, "common.version_unknown")
        _answers(
            repo,
            {
                "messages:shared": "<script>neu</script>",
                "component:common.version_unknown": "<script>neu</script>",
            },
        )
        assert repo.run().code == 2
        assert repo.settings("de")["messages"]["shared"] == "alt"
        assert repo.component("de")["common"]["version_unknown"] == "unbekannt"
        # Neither is recorded as done, so the next run owes both again.
        assert _planned(repo.run("--dry-run")) == {
            "de/messages/shared",
            "de/component/common.version_unknown",
        }

    def test_fully_stale_shared_group_is_translated_instead_of_reused(
        self, repo: LocaleRepo
    ) -> None:
        repo.write_settings("de", {"shared": "alt"})
        repo.stale(SETTINGS_SURFACE, "messages.shared")
        repo.stale(COMPONENT_SURFACE, "common.version_unknown")
        _answers(
            repo,
            {
                "messages:shared": "neu",
                "component:common.version_unknown": "anders",
            },
        )
        assert repo.run().code == 0
        assert sorted(list(request.strings) for request in repo.engine.requests) == [
            ["component:common.version_unknown"],
            ["messages:shared"],
        ]
        assert repo.settings("de")["messages"]["shared"] == "neu"
        assert repo.component("de")["common"]["version_unknown"] == "neu"
        assert _planned(repo.run("--dry-run")) == set()


class TestApplyWrites:
    @pytest.fixture(autouse=True)
    def _catalogs(self, repo: LocaleRepo) -> None:
        repo.write_settings(
            "en", {"first": "One", "second": "Two"}, native_name="English", tools=False
        )
        # Deliberately not in en.json order; the orphan must go.
        repo.write_settings("de", {"second": "Zwei", "orphan": "Weg"})
        _answers(repo, {"messages:first": "Eins"})

    def test_settings_writes_mirror_en_order_and_drop_orphans(
        self, repo: LocaleRepo
    ) -> None:
        assert repo.run().code == 0
        messages: dict[str, Any] = repo.settings("de")["messages"]
        assert list(messages) == ["first", "second"]
        assert messages["first"] == "Eins"
        assert "orphan" not in messages

    def test_apply_is_idempotent(self, repo: LocaleRepo) -> None:
        """A second no-work pass must not rewrite the file — this is the
        property that keeps the CI auto-commit loop from ping-ponging."""
        assert repo.run().code == 0
        catalog = repo.settings_dir / "de.json"
        settled = catalog.read_bytes()
        run = repo.run()
        assert run.code == 0, run
        assert catalog.read_bytes() == settled
        assert "de.json" not in run.out
