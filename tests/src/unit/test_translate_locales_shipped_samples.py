"""Every shipped catalog gives the engine samples of how it addresses its reader.

The style samples are the only signal the engine gets about a catalog's
register — du/Sie, tu/vous, 你/您 — and nothing downstream can recover it. The
guarantee has to hold for what actually ships, not just for a fixture: each
test copies the English catalog and one shipped catalog into a scratch
repository, runs ``scripts/translate_locales.py`` with one string owed on that
surface, and reads the samples the run picked.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ha_mcp.settings_ui.locale_policy import BEST_EFFORT_LOCALES

from ._locale_sync_repo import (
    COMPONENT_SURFACE,
    REPO_ROOT,
    SETTINGS_SURFACE,
    LocaleRepo,
    Run,
)

# English that speaks to the reader: what a style sample must carry for the
# engine to see how a catalog addresses its reader.
_ADDRESSES_READER = re.compile(r"\byou(?:|r|rs|rself|rselves)\b", re.IGNORECASE)


def _has_text(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_text(item) for item in value.values())
    return isinstance(value, str)


def _shipped(surface: str, text: Any) -> list[str]:
    """Shipped catalogs on ``surface`` that carry translated text.

    A catalog with no text yet has no wording to sample, so sampling it would
    fail on emptiness rather than on the property checked here. Such a catalog
    cannot ship anyway — the `Decision` and `PredicateOp` word checks in
    test_settings_ui_i18n.py apply to every settings catalog, and the locale
    README allows an empty starting component catalog — so the exclusion
    narrows the check rather than opening a way past it.
    """
    return sorted(
        path.stem
        for path in (REPO_ROOT / surface).glob("*.json")
        if path.stem != "en"
        and path.stem not in BEST_EFFORT_LOCALES
        and _has_text(text(json.loads(path.read_text("utf-8"))))
    )


_TRANSLATED_LOCALES = _shipped(SETTINGS_SURFACE, lambda c: c.get("messages", {}))
_COMPONENT_LOCALES = _shipped(COMPONENT_SURFACE, lambda c: c)


@pytest.fixture
def repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> LocaleRepo:
    return LocaleRepo(tmp_path, monkeypatch, capsys)


class _Shipped:
    """One shipped catalog in the scratch repository, beside the real English."""

    def __init__(self, repo: LocaleRepo, locale: str, surface: str) -> None:
        self.repo = repo
        self.locale = locale
        self.surface = surface
        self.section = "component" if surface == COMPONENT_SURFACE else "messages"
        for directory in (repo.settings_dir, repo.component_dir):
            (directory / "de.json").unlink()
            (directory / "en.json").write_bytes(
                (REPO_ROOT / directory.relative_to(repo.root) / "en.json").read_bytes()
            )
        # The run walks the locales that have a settings catalog.
        settings = REPO_ROOT / SETTINGS_SURFACE / f"{locale}.json"
        (repo.settings_dir / settings.name).write_bytes(settings.read_bytes())
        if self.section == "component":
            # The one string the run owes on this surface. It addresses nobody,
            # and no settings string shares it, which would supply its wording.
            english = repo.component("en") | {"owed": "Owed by the scratch run."}
            repo.write_component("en", english)
        self.catalog: dict[str, Any] = json.loads(
            (REPO_ROOT / surface / f"{locale}.json").read_text("utf-8")
        )

    def samples(self, queued: str | None = None) -> tuple[list[str], list[str]]:
        """The keys the run sampled and their English, with ``queued`` owed."""
        catalog = json.loads(json.dumps(self.catalog))
        if queued is not None:
            node = (
                catalog.get("messages", {}) if self.section == "messages" else catalog
            )
            *parents, leaf = (
                queued.split(".") if self.section == "component" else [queued]
            )
            for part in parents:
                node = node[part]
            del node[leaf]
        self.repo.write_json(
            self.repo.root / self.surface / f"{self.locale}.json", catalog
        )
        self.repo.engine.requests.clear()
        run = self.repo.run()
        assert run.code == 0, run
        return self._keys(run), self._english()

    def _keys(self, run: Run) -> list[str]:
        match = re.search(
            rf"^  {re.escape(self.locale)}/{self.section}: register samples (\[.*\])$",
            run.out,
            re.MULTILINE,
        )
        if match is None:
            assert f"{self.locale}/{self.section}: no sample" in run.err, run
            return []
        keys: list[str] = ast.literal_eval(match.group(1))
        return keys

    def _english(self) -> list[str]:
        """The English of the sample pairs the surface's first request showed."""
        component = self.section == "component"
        prompt = next(
            request.prompt
            for request in self.repo.engine.requests
            if all(sid.startswith("component:") for sid in request.strings) is component
        )
        return [line[4:] for line in prompt.splitlines() if line.startswith("EN: ")]


@pytest.mark.parametrize(
    ("locale", "surface"),
    [(locale, SETTINGS_SURFACE) for locale in _TRANSLATED_LOCALES]
    + [(locale, COMPONENT_SURFACE) for locale in _COMPONENT_LOCALES],
)
def test_every_shipped_catalog_gets_reader_addressing_samples(
    repo: LocaleRepo, locale: str, surface: str
) -> None:
    """Every shipped catalog must yield samples, and every one of them must
    address the reader.

    The component surface carries the same guarantee and less margin. A
    settings catalog samples from hundreds of keys, so one English string
    losing its second person costs it one candidate. A component catalog
    starts empty and is filled a key at a time, so early on the whole surface
    can rest on a single shared key. Lose the addressing there and the engine
    is told nothing about how this language addresses its reader and falls
    back to its own register for every later string, and the only trace is a
    line on stderr inside an unattended workflow run. The component request
    samples the component catalog, never the settings one: the two surfaces
    need not share a register.
    """
    keys, english = _Shipped(repo, locale, surface).samples()
    assert keys, (
        f"{surface}/{locale}.json yields no style sample, so the engine gets "
        "no signal about how this catalog addresses its reader"
    )
    assert len(english) == len(keys)
    assert all(_ADDRESSES_READER.search(text) for text in english), (
        f"{surface}/{locale}.json samples {keys} do not address the reader"
    )


@pytest.mark.parametrize(
    ("locale", "surface"),
    [(locale, SETTINGS_SURFACE) for locale in _TRANSLATED_LOCALES]
    + [(locale, COMPONENT_SURFACE) for locale in _COMPONENT_LOCALES],
)
def test_samples_survive_their_own_anchor_being_queued(
    repo: LocaleRepo, locale: str, surface: str
) -> None:
    """The run excludes what it is about to rewrite.

    A catalog whose only reader-addressing key is itself queued for
    retranslation sends a request with neither a tone sample nor the
    register rule, both of which ship only with samples. One anchor short of
    the sample count is enough to be safe: losing any single candidate still
    leaves one to imitate. A settings catalog loses its last candidate only
    by a rewording of the English; a component catalog is filled a key at a
    time and can hold its whole register on the one key that arrived first,
    and rewording that key is what queues it.
    """
    shipped = _Shipped(repo, locale, surface)
    anchors, _english = shipped.samples()
    for queued in anchors:
        keys, _english = shipped.samples(queued)
        assert keys, (
            f"{surface}/{locale}.json falls back to no style sample at all once "
            f"{queued!r} is queued for retranslation, which is exactly the run "
            "that would have needed one"
        )


def test_the_shipped_catalogs_are_discovered() -> None:
    """The checks above run over a glob, and an empty glob would collapse
    them to a silent skip rather than a failure."""
    assert _TRANSLATED_LOCALES, "no translated settings catalogs found"
    assert _COMPONENT_LOCALES, "no component catalogs found"
