"""A scratch repository that runs ``scripts/translate_locales.py`` from its command line.

The pipeline finds everything from its own location: the catalogs, the
English-source baseline and the progress record beside the locale check it
loads, and the derived catalogs ``generate_locales`` rewrites. Copies of the
scripts and of that check run inside a temporary tree, so a run reads and
writes only the files a test wrote there. The Gemini API answers at httpx's
transport, the layer below the script: its request, retries and response
parsing all run.
"""

from __future__ import annotations

import json
import runpy
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Surface names as the baseline files them.
SETTINGS_SURFACE = "src/ha_mcp/settings_ui/locales"
COMPONENT_SURFACE = "custom_components/ha_mcp_tools/translations"
TOOLS_SURFACE = "settings UI tool titles and descriptions"

# The modules the pipeline imports by name from beside itself, and the name it
# loads the locale check under. One imported from the real tree by an earlier
# test would answer for the scratch copy, and write the real tree's files.
_SIBLINGS = (
    "translate_locales",
    "generate_locales",
    "locale_rules",
    "update_locale_baseline",
    "extract_tools",
    "_locale_parity",
)

_COPIED = (
    "scripts/translate_locales.py",
    "scripts/generate_locales.py",
    "scripts/locale_rules.py",
    "scripts/update_locale_baseline.py",
    "scripts/extract_tools.py",
    "tests/src/unit/test_locale_parity.py",
    "src/ha_mcp/tools/tool_hints.py",
    # The on-screen names a translation has to keep English are read from these.
    "custom_components/ha_mcp_tools/config_flow.py",
    "custom_components/ha_mcp_tools/const.py",
)

# One tool, so the settings catalogs have a tool row and a group heading.
SAMPLE_TOOL = '''
@mcp.tool(tags={"Sample"}, annotations={"title": "Sample Tool"})
async def ha_sample() -> None:
    """Read the sample."""
'''
SAMPLE_TOOL_TRANSLATED = {
    "tools": {"ha_sample": {"title": "Beispielwerkzeug", "description": "Liest."}},
    "tool_groups": {"Sample": "Beispiel"},
}

_FEATURE_FLAGS_JS = (
    "// FEATURE_META:BEGIN GENERATED (scripts/generate_locales.py)\n"
    "// FEATURE_META:END GENERATED\n"
)

_STRINGS_HEADER = "Strings to translate (JSON):\n"


@dataclass(frozen=True)
class Request:
    """One request the pipeline sent the engine."""

    url: str
    body: dict[str, Any]
    prompt: str
    # The strings the prompt asks for, by the identifier they travel under.
    strings: dict[str, str]

    @property
    def is_retry(self) -> bool:
        return "Your previous attempt was rejected" in self.prompt


def answer(translations: dict[str, Any]) -> httpx.Response:
    """A 200 whose candidate text is ``translations`` as a JSON object."""
    return envelope(
        {"candidates": [{"content": {"parts": [{"text": json.dumps(translations)}]}}]}
    )


def envelope(payload: Any) -> httpx.Response:
    """A 200 carrying ``payload`` as its JSON body."""
    return httpx.Response(200, json=payload)


def echo(request: Request) -> httpx.Response:
    """Answers every string with its English, which every check accepts."""
    return answer(request.strings)


@dataclass
class Engine:
    """The Gemini API: records each request and answers it with ``reply``."""

    reply: Callable[[Request], httpx.Response] = echo
    requests: list[Request] = field(default_factory=list)

    @property
    def prompts(self) -> list[str]:
        return [request.prompt for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = str(body["contents"][0]["parts"][0]["text"])
        strings, _end = json.JSONDecoder().raw_decode(
            prompt.split(_STRINGS_HEADER, 1)[1]
        )
        sent = Request(str(request.url), body, prompt, strings)
        self.requests.append(sent)
        response = self.reply(sent)
        response.request = request
        return response


class Clock:
    """The monotonic clock the time budget reads, stopped until moved."""

    def __init__(self) -> None:
        self.now = 1e12  # no real monotonic reading gets here

    def __call__(self) -> float:
        return self.now

    def spend_the_budget(self) -> None:
        self.now += 1e9


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str


class LocaleRepo:
    """The scratch tree, its engine, its clock and the waits a run made."""

    def __init__(
        self,
        root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self.root = root / "repo"
        self._monkeypatch = monkeypatch
        self._capsys = capsys
        for relative in _COPIED:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / relative, target)
        self.unit_dir.joinpath("locale_shared_places.json").write_text("{}", "utf-8")
        self.write_tools(SAMPLE_TOOL)
        js = self.root / "src/ha_mcp/settings_ui/settings_js/05_feature_flags.js"
        js.parent.mkdir(parents=True)
        js.write_text(_FEATURE_FLAGS_JS, encoding="utf-8")
        for flavor in ("homeassistant-addon", "homeassistant-addon-dev"):
            (self.root / flavor / "translations").mkdir(parents=True)
            (self.root / flavor / "config.yaml").write_text("schema: {}\n", "utf-8")
        self.settings_dir.mkdir(parents=True)
        self.component_dir.mkdir(parents=True, exist_ok=True)
        self.write_settings("en", {}, native_name="English", tools=False)
        self.write_settings("de", {})
        self.write_component("en", {})
        self.write_component("de", {})
        self.write_baseline({})

        self.engine = Engine()
        self.clock = Clock()
        self.sleeps: list[float] = []
        engine = self.engine

        def handle_request(
            _transport: httpx.HTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            return engine.handle(request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
        monkeypatch.setattr(time, "monotonic", self.clock)
        monkeypatch.setattr(time, "sleep", self.sleeps.append)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("GEMINI_API_URL", raising=False)
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        monkeypatch.setenv("TRANSLATE_TIME_BUDGET_SECONDS", "3600")

    @property
    def unit_dir(self) -> Path:
        return self.root / "tests/src/unit"

    @property
    def settings_dir(self) -> Path:
        return self.root / SETTINGS_SURFACE

    @property
    def component_dir(self) -> Path:
        return self.root / COMPONENT_SURFACE

    @property
    def baseline_path(self) -> Path:
        return self.unit_dir / "locale_source_baseline.json"

    @property
    def progress_path(self) -> Path:
        return self.unit_dir / "locale_sync_progress.json"

    def write_tools(self, source: str) -> None:
        tools = self.root / "src/ha_mcp/tools/tools_sample.py"
        tools.write_text(source, encoding="utf-8")

    def write_settings(
        self,
        locale: str,
        messages: dict[str, str] | None,
        *,
        native_name: str = "Deutsch",
        tools: bool = True,
    ) -> None:
        """A settings catalog; ``tools`` adds the sample tool's translation."""
        catalog: dict[str, Any] = {"meta": {"native_name": native_name, "dir": "ltr"}}
        if messages is not None:
            catalog["messages"] = messages
        catalog |= SAMPLE_TOOL_TRANSLATED if tools else {"tools": {}, "tool_groups": {}}
        self.write_json(self.settings_dir / f"{locale}.json", catalog)

    def write_component(self, locale: str, catalog: dict[str, Any]) -> None:
        self.write_json(self.component_dir / f"{locale}.json", catalog)

    def write_baseline(self, baseline: dict[str, dict[str, str]]) -> None:
        self.write_json(self.baseline_path, baseline)

    def stale(self, surface: str, *keys: str) -> None:
        """Pin ``keys`` to English that is no longer current."""
        baseline = self.read_json(self.baseline_path)
        baseline.setdefault(surface, {}).update(dict.fromkeys(keys, "0" * 16))
        self.write_baseline(baseline)

    def settings(self, locale: str) -> dict[str, Any]:
        return self.read_json(self.settings_dir / f"{locale}.json")

    def component(self, locale: str) -> dict[str, Any]:
        return self.read_json(self.component_dir / f"{locale}.json")

    @staticmethod
    def write_json(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def run(self, *argv: str) -> Run:
        """``python scripts/translate_locales.py <argv>`` in this tree."""
        script = self.root / "scripts" / "translate_locales.py"
        saved = {
            name: sys.modules.pop(name) for name in _SIBLINGS if name in sys.modules
        }
        saved_path = list(sys.path)
        self._monkeypatch.setattr(sys, "argv", [str(script), *argv])
        self._capsys.readouterr()
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
        else:
            raise AssertionError("the script ended without an exit status")
        finally:
            sys.path[:] = saved_path
            for name in _SIBLINGS:
                sys.modules.pop(name, None)
            sys.modules.update(saved)
        out, err = self._capsys.readouterr()
        assert isinstance(code, int), f"exit status {code!r}: {err}"
        return Run(code, out, err)


@pytest.fixture
def repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> LocaleRepo:
    """A locale repository whose English settings catalog has one string."""
    repo = LocaleRepo(tmp_path, monkeypatch, capsys)
    repo.write_settings("en", {"greeting": "Hello"}, native_name="English", tools=False)
    return repo
