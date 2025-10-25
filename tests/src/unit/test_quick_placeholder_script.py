import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

if "httpx" not in sys.modules:
    httpx_stub = types.ModuleType("httpx")

    class _AsyncClient:  # pragma: no cover - stubbed for import compatibility
        def __init__(self, *args, **kwargs):
            pass

        async def request(self, *args, **kwargs):
            raise RuntimeError("httpx stub does not perform HTTP requests")

        async def aclose(self) -> None:
            return None

    class _Timeout:  # pragma: no cover - stub
        def __init__(self, *args, **kwargs):
            pass

    class _HttpxError(Exception):
        pass

    httpx_stub.AsyncClient = _AsyncClient
    httpx_stub.Timeout = _Timeout
    httpx_stub.ConnectError = _HttpxError
    httpx_stub.TimeoutException = _HttpxError
    httpx_stub.HTTPError = _HttpxError
    sys.modules["httpx"] = httpx_stub

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")

    def _load_dotenv(*args, **kwargs):  # pragma: no cover - stub
        return False

    dotenv_stub.load_dotenv = _load_dotenv
    sys.modules["dotenv"] = dotenv_stub

if "pydantic" not in sys.modules:
    pydantic_stub = types.ModuleType("pydantic")

    class ValidationError(Exception):
        def __init__(self, errors=None):
            super().__init__("Validation error")
            self._errors = errors or []

        def errors(self):  # pragma: no cover - stub helper
            return self._errors

    def Field(*args, **kwargs):  # pragma: no cover - stub
        return None

    def field_validator(*args, **kwargs):  # pragma: no cover - stub decorator
        def decorator(func):
            return func

        return decorator

    class BaseModel:
        def __init__(self, **data):  # pragma: no cover - stub initialiser
            annotations = getattr(self, "__annotations__", {})
            for key in annotations:
                setattr(self, key, data.get(key))

        @classmethod
        def model_validate(cls, data):
            if isinstance(data, cls):
                return data
            if not isinstance(data, dict):
                raise ValidationError([{"msg": "data must be a dict"}])
            try:
                return cls(**data)
            except TypeError as exc:  # pragma: no cover - error path
                raise ValidationError([{"msg": str(exc)}]) from exc

        def dict(self, *args, **kwargs):  # pragma: no cover - stub helper
            annotations = getattr(self, "__annotations__", {})
            return {key: getattr(self, key) for key in annotations}

    pydantic_stub.BaseModel = BaseModel
    pydantic_stub.Field = Field
    pydantic_stub.ValidationError = ValidationError
    pydantic_stub.field_validator = field_validator
    sys.modules["pydantic"] = pydantic_stub

if "pydantic_settings" not in sys.modules:
    settings_stub = types.ModuleType("pydantic_settings")

    class BaseSettings:  # pragma: no cover - stub
        model_config = {}

        def model_dump(self, *args, **kwargs):
            return {}

    class SettingsConfigDict(dict):  # pragma: no cover - stub alias
        pass

    settings_stub.BaseSettings = BaseSettings
    settings_stub.SettingsConfigDict = SettingsConfigDict
    sys.modules["pydantic_settings"] = settings_stub

if "websockets" not in sys.modules:
    websockets_stub = types.ModuleType("websockets")

    class _WebSocketClientProtocol:  # pragma: no cover - stub
        async def send(self, *args, **kwargs):
            raise RuntimeError("websockets stub")

        async def recv(self):  # pragma: no cover - stub
            raise RuntimeError("websockets stub")

    async def connect(*args, **kwargs):  # pragma: no cover - stub
        return _WebSocketClientProtocol()

    websockets_stub.connect = connect
    websockets_stub.WebSocketClientProtocol = _WebSocketClientProtocol
    sys.modules["websockets"] = websockets_stub

if "fastmcp" not in sys.modules:
    fastmcp_stub = types.ModuleType("fastmcp")

    class FastMCP:  # pragma: no cover - stub
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, func):
            return func

        async def run_async(self):  # pragma: no cover - stub
            return None

    fastmcp_stub.FastMCP = FastMCP
    sys.modules["fastmcp"] = fastmcp_stub

if "textdistance" not in sys.modules:
    textdistance_stub = types.ModuleType("textdistance")

    class Levenshtein:  # pragma: no cover - stub
        @staticmethod
        def distance(a: str, b: str) -> int:
            return abs(len(a) - len(b))

    textdistance_stub.Levenshtein = Levenshtein
    sys.modules["textdistance"] = textdistance_stub

from ha_mcp.tools.tools_quick_placeholder import (
    QuickPlaceholderScriptExecutor,
    is_obvious_match,
    normalize_confidence,
    normalize_search_terms,
)


class DummyClient:
    """Minimal async client stub for unit testing."""

    def __init__(self, states: list[dict], scripts: dict[str, dict]):
        self._states = states
        self._scripts = scripts
        self.service_calls: list[tuple[str, str, dict]] = []

    async def get_states(self) -> list[dict]:  # pragma: no cover - simple proxy
        return self._states

    async def get_script_config(self, script_id: str) -> dict:
        return self._scripts.get(script_id, {})

    async def call_service(self, domain: str, service: str, data: dict) -> dict:
        self.service_calls.append((domain, service, data))
        return {"domain": domain, "service": service, "data": data}


def test_normalize_search_terms_with_strings() -> None:
    terms, raw = normalize_search_terms(["kitchen", "living room"])
    assert len(terms) == 2
    assert pytest.approx(sum(term.weight for term in terms), rel=1e-6) == 1.0
    assert raw == ["kitchen", "living room"]


def test_normalize_search_terms_with_weights() -> None:
    terms, raw = normalize_search_terms([
        {"value": "Kitchen", "weight": 0.25},
        {"value": "Salon", "weight": 0.75},
    ])
    assert [term.value for term in terms] == ["Kitchen", "Salon"]
    assert pytest.approx(terms[0].weight, rel=1e-6) == 0.25
    assert pytest.approx(terms[1].weight, rel=1e-6) == 0.75
    assert raw[0]["weight"] == 0.25


def test_normalize_confidence_variants() -> None:
    ratio, percent, source = normalize_confidence(None, default_ratio=0.6)
    assert ratio == pytest.approx(0.6)
    assert percent == pytest.approx(60)
    assert source == "default"

    ratio, percent, source = normalize_confidence(0.8)
    assert ratio == pytest.approx(0.8)
    assert percent == pytest.approx(80)
    assert source == "float"

    ratio, percent, source = normalize_confidence(85)
    assert ratio == pytest.approx(0.85)
    assert percent == pytest.approx(85)
    assert source == "int"


def test_is_obvious_match_prefers_cache() -> None:
    candidates = [
        {"entity_id": "light.one", "score": 90.0},
        {"entity_id": "light.two", "score": 90.0},
    ]
    match, reason = is_obvious_match(candidates, threshold_ratio=0.5, cached_entity_id="light.two")
    assert match is not None
    assert match["entity_id"] == "light.two"
    assert reason == "cache_preference"


def test_executor_resolves_and_runs_script() -> None:
    states = [
        {
            "entity_id": "light.kitchen_main",
            "state": "off",
            "attributes": {"friendly_name": "Kitchen Main Light", "area_id": "kitchen"},
        },
        {
            "entity_id": "light.living_room",
            "state": "off",
            "attributes": {"friendly_name": "Living Room Lamp", "area_id": "living_room"},
        },
    ]

    manifest = {
        "placeholder_manifest": {
            "placeholders": [
                {
                    "id": "TARGET_LIGHT",
                    "search_terms": ["Kitchen Main"],
                    "min_confidence": 0.7,
                }
            ],
            "min_confidence": 0.65,
            "limit": 5,
        }
    }

    client = DummyClient(states=states, scripts={"script.movie_time": manifest})
    executor = QuickPlaceholderScriptExecutor(client)

    result = asyncio.run(
        executor.execute("script.movie_time", None, {"brightness_pct": 30}, None)
    )

    assert result["status"] == "resolved"
    assert result["resolved_entities"] == {"TARGET_LIGHT": "light.kitchen_main"}
    assert client.service_calls[0][0] == "script"
    assert client.service_calls[0][2]["fields"]["TARGET_LIGHT"] == "light.kitchen_main"


def test_executor_requests_elicitation_when_ambiguous() -> None:
    states = [
        {
            "entity_id": "light.shared_a",
            "state": "off",
            "attributes": {"friendly_name": "Shared Light", "area_id": "studio"},
        },
        {
            "entity_id": "light.shared_b",
            "state": "off",
            "attributes": {"friendly_name": "Shared Light", "area_id": "studio"},
        },
    ]

    manifest = {
        "placeholder_manifest": {
            "placeholders": [
                {
                    "id": "STUDIO_LIGHT",
                    "search_terms": ["Shared Light"],
                    "min_confidence": 0.6,
                }
            ],
        }
    }

    client = DummyClient(states=states, scripts={"script.shared_scene": manifest})
    executor = QuickPlaceholderScriptExecutor(client)

    result = asyncio.run(executor.execute("script.shared_scene", None, None, None))

    assert result["status"] == "elicitation"
    assert result["elicitation"]["placeholder_id"] == "STUDIO_LIGHT"
    assert "light.shared_a" in result["elicitation"]["allowed_responses"]["select"]


def test_executor_respects_elicitation_round_limit() -> None:
    states = [
        {
            "entity_id": "light.shared_a",
            "state": "off",
            "attributes": {"friendly_name": "Shared Light", "area_id": "studio"},
        },
        {
            "entity_id": "light.shared_b",
            "state": "off",
            "attributes": {"friendly_name": "Shared Light", "area_id": "studio"},
        },
    ]

    manifest = {
        "placeholder_manifest": {
            "placeholders": [
                {
                    "id": "STUDIO_LIGHT",
                    "search_terms": ["Shared Light"],
                }
            ],
        }
    }

    client = DummyClient(states=states, scripts={"script.shared_scene": manifest})
    executor = QuickPlaceholderScriptExecutor(client)

    result = asyncio.run(
        executor.execute(
            "script.shared_scene",
            None,
            None,
            {"rounds_used": 2},
        )
    )

    assert result["status"] == "failed"
    assert "Exceeded" in result["error"]
