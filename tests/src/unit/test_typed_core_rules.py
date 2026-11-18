"""The ast-grep rules that refuse a Core object or WebSocket frame typed into a test.

A state, registry row, config entry or frame comes from the real Core; a dict
of that shape written into a test is a guess of what Core holds or sends.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ._rule_scan import REPO, RULES, flagged, scan

_OBJECTS = "no-typed-core-objects"
_FRAMES = "no-typed-core-frames"


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param(
            'S = {"entity_id": "light.a", "state": "on", "attributes": {}}\n',
            True,
            id="state",
        ),
        pytest.param(
            'R = {"entity_id": "light.a", "unique_id": "a", "platform": "demo"}\n',
            True,
            id="registry-row-naming-its-platform",
        ),
        pytest.param(
            'D = {"id": "d", "identifiers": [["x", "1"]]}\n', True, id="device"
        ),
        pytest.param('E = {"entry_id": "e", "title": "T"}\n', True, id="config-entry"),
        pytest.param(
            'T = {"trigger": "state", "entity_id": "light.a", "to": "on"}\n',
            False,
            id="trigger-config",
        ),
        pytest.param(
            'C = {"condition": "state", "entity_id": "light.a", "state": "on"}\n',
            False,
            id="condition-config",
        ),
        pytest.param(
            'P = {"platform": "state", "entity_id": "light.a", "to": "on"}\n',
            False,
            id="legacy-trigger-config",
        ),
        pytest.param(
            'N = {"entity_id": "light.a", "state": "on"}\n', False, id="scene-entity"
        ),
        pytest.param(
            'A = {"domain": "zha", "entry_id": "e"}\n', False, id="tool-arguments"
        ),
        pytest.param(
            'assert got == {"entity_id": "light.a", "state": "on", "attributes": {}}\n',
            False,
            id="expected-in-an-assert",
        ),
        pytest.param(
            'hass_storage[er.STORAGE_KEY] = {"data": {"entities": [\n'
            '    {"entity_id": "light.a", "unique_id": "a", "platform": "gone"}]}}\n',
            False,
            id="registry-file-core-loads",
        ),
        pytest.param(
            'S = dict(entity_id="light.a", state="on", attributes={})\n',
            True,
            id="state-built-by-dict",
        ),
        pytest.param(
            'assert summary({"entity_id": "light.a", "state": "on", "context": c})\n',
            True,
            id="state-an-assert-hands-to-a-call",
        ),
        pytest.param(
            'assert got == dict(entity_id="light.a", state="on", attributes={})\n',
            False,
            id="expected-built-by-dict",
        ),
        pytest.param(
            'set_entity(entity_id="light.a", unique_id="a")\n',
            False,
            id="keywords-of-another-call",
        ),
    ],
)
def test_only_a_typed_core_object_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """A state, registry row or config entry comes from Core; a user's config,
    a tool's arguments, an expectation and a file Core loads do not."""
    assert flagged(_OBJECTS, source, tmp_path) is refused


@pytest.mark.parametrize(
    ("source", "refused"),
    [
        pytest.param(
            'R = {"id": 1, "type": "result", "success": True}\n', True, id="result"
        ),
        pytest.param('E = {"id": 1, "type": "event", "event": {}}\n', True, id="event"),
        pytest.param(
            'A = {"type": "auth_ok", "ha_version": "1"}\n', True, id="auth-ok"
        ),
        pytest.param(
            'C = {"id": 1, "type": "subscribe_events"}\n', False, id="command-to-core"
        ),
        pytest.param(
            'L = {"type": "auth", "access_token": token}\n', False, id="login-to-core"
        ),
        pytest.param(
            'assert answer == {"success": True, "type": "pong"}\n',
            False,
            id="expected-in-an-assert",
        ),
        pytest.param(
            'R = dict(id=1, type="result", success=True)\n', True, id="built-by-dict"
        ),
        pytest.param(
            'assert handle({"type": "event", "event": {}})\n',
            True,
            id="frame-an-assert-hands-to-a-call",
        ),
        pytest.param(
            'assert answer == dict(success=True, type="pong")\n',
            False,
            id="expected-built-by-dict",
        ),
        pytest.param('send(type="result")\n', False, id="keyword-of-another-call"),
    ],
)
def test_only_a_typed_core_frame_is_refused(
    source: str, refused: bool, tmp_path: Path
) -> None:
    """What Core sends over its WebSocket comes from Core; what a client sends
    it, and an expectation of Core's reply, do not."""
    assert flagged(_FRAMES, source, tmp_path) is refused


@pytest.mark.parametrize("rule", [_OBJECTS, _FRAMES])
def test_no_test_types_a_core_object_or_frame(rule: str) -> None:
    """Neither rule has a grandfather list: every unit test complies."""
    path = RULES / f"{rule}.yml"
    assert "ignores" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    assert scan(path, REPO, "tests") == []
