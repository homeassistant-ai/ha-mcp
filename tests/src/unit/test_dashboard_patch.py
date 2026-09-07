"""Behavioral contract for the independently shipped dashboard patch evaluators."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SERVER = _ROOT / "src/ha_mcp/utils/dashboard_patch.py"
_COMPONENT = _ROOT / "custom_components/ha_mcp_tools/dashboard_patch.py"


@pytest.fixture(params=[_SERVER, _COMPONENT], ids=["server", "component"])
def apply_patch(request: pytest.FixtureRequest) -> Callable[..., dict[str, Any]]:
    """Load either standalone file without importing Home Assistant packages."""
    path = request.param
    assert path.is_file(), f"Dashboard patch evaluator is missing: {path.name}"
    spec = importlib.util.spec_from_file_location("dashboard_patch_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.apply_dashboard_patch


def test_component_copy_matches_server_bytes() -> None:
    """Standalone installs must ship the same semantics as the server."""
    assert _SERVER.is_file() and _COMPONENT.is_file()
    assert _SERVER.read_bytes() == _COMPONENT.read_bytes()


def test_operations_apply_in_order_without_mutating_inputs(apply_patch) -> None:
    config = {"title": "Before", "views": [{"title": "Main", "cards": []}]}
    patch = [
        {"op": "test", "path": "/title", "value": "Before"},
        {"op": "replace", "path": "/title", "value": "After"},
        {"op": "add", "path": "/views/0/cards/-", "value": {"type": "markdown"}},
        {"op": "remove", "path": "/views/0/title"},
    ]
    original_config, original_patch = deepcopy(config), deepcopy(patch)

    result = apply_patch(config, patch)

    assert result == {"title": "After", "views": [{"cards": [{"type": "markdown"}]}]}
    assert config == original_config
    assert patch == original_patch
    result["views"][0]["cards"][0]["type"] = "entities"
    assert config == original_config
    assert patch == original_patch


def test_pointer_escaping_empty_keys_and_object_numeric_keys(apply_patch) -> None:
    config = {"a/b": {"~key": 1}, "~1": 2, "": {"": 3}, "01": 4, "-": 5}
    result = apply_patch(
        config,
        [
            {"op": "replace", "path": "/a~1b/~0key", "value": "escaped"},
            {"op": "replace", "path": "/~01", "value": "decode once"},
            {"op": "replace", "path": "//", "value": "empty"},
            {"op": "replace", "path": "/01", "value": "object key"},
            {"op": "replace", "path": "/-", "value": "dash key"},
        ],
    )
    assert result == {
        "a/b": {"~key": "escaped"},
        "~1": "decode once",
        "": {"": "empty"},
        "01": "object key",
        "-": "dash key",
    }


def test_array_insert_append_replace_and_remove(apply_patch) -> None:
    result = apply_patch(
        {"cards": ["first", "last"]},
        [
            {"op": "add", "path": "/cards/1", "value": "middle"},
            {"op": "add", "path": "/cards/3", "value": "end"},
            {"op": "add", "path": "/cards/-", "value": "appended"},
            {"op": "replace", "path": "/cards/0", "value": "new first"},
            {"op": "remove", "path": "/cards/2"},
        ],
    )
    assert result == {"cards": ["new first", "middle", "end", "appended"]}


@pytest.mark.parametrize("index", ["-1", "+1", "01", "1.0", " 1", "١", "", "3"])
def test_add_rejects_invalid_array_indexes(apply_patch, index) -> None:
    with pytest.raises(ValueError):
        apply_patch({"cards": [1, 2]}, [{"op": "add", "path": f"/cards/{index}", "value": 0}])


@pytest.mark.parametrize("op", ["remove", "replace", "test"])
@pytest.mark.parametrize("index", ["-", "2", "-1", "01"])
def test_existing_array_targets_are_required(apply_patch, op, index) -> None:
    with pytest.raises(ValueError):
        apply_patch({"cards": [1, 2]}, [{"op": op, "path": f"/cards/{index}", "value": 0}])


@pytest.mark.parametrize("op", ["remove", "replace", "test"])
def test_existing_object_targets_are_required(apply_patch, op) -> None:
    with pytest.raises(ValueError):
        apply_patch({}, [{"op": op, "path": "/missing", "value": None}])


@pytest.mark.parametrize("path", ["title", "#/title", "/bad~", "/bad~2", "/~x/title", None, 1])
def test_malformed_pointer_is_rejected(apply_patch, path) -> None:
    with pytest.raises(ValueError):
        apply_patch({}, [{"op": "add", "path": path, "value": 1}])


@pytest.mark.parametrize("config,path", [({}, "/missing/child"), ({"value": 1}, "/value/child"), ({"cards": []}, "/cards/-/child")])
def test_add_does_not_create_missing_or_scalar_parents(apply_patch, config, path) -> None:
    with pytest.raises(ValueError):
        apply_patch(config, [{"op": "add", "path": path, "value": 1}])


def test_add_replaces_existing_object_member_and_accepts_null(apply_patch) -> None:
    assert apply_patch({"title": "Old"}, [{"op": "add", "path": "/title", "value": None}]) == {"title": None}


@pytest.mark.parametrize("actual,expected", [(True, 1), (False, 0), (1, 1.0), ([True], [1]), ({"nested": False}, {"nested": 0}), ("1", 1), ([1, 2], [2, 1])])
def test_test_operation_rejects_type_or_value_mismatch(apply_patch, actual, expected) -> None:
    with pytest.raises(ValueError):
        apply_patch({"value": actual}, [{"op": "test", "path": "/value", "value": expected}])


def test_test_operation_accepts_equal_nested_values_and_object_key_order(apply_patch) -> None:
    config = {"value": {"a": [None, True, 1, 1.5], "b": "text"}}
    assert apply_patch(config, [{"op": "test", "path": "/value", "value": {"b": "text", "a": [None, True, 1, 1.5]}}]) == config


@pytest.mark.parametrize("last_op", [{"op": "remove", "path": "/missing"}, {"op": "test", "path": "/views/0/title", "value": "wrong"}, {"op": "move", "path": "/views"}])
def test_failure_after_mutation_leaves_original_unchanged(apply_patch, last_op) -> None:
    config = {"views": [{"title": "original"}]}
    original = deepcopy(config)
    with pytest.raises(ValueError):
        apply_patch(config, [{"op": "replace", "path": "/views/0/title", "value": "changed"}, last_op])
    assert config == original


@pytest.mark.parametrize("op", ["add", "replace"])
def test_root_replacement_returns_independent_dict(apply_patch, op) -> None:
    config = {"title": "old"}
    replacement = {"views": [{"title": "new"}]}
    result = apply_patch(config, [{"op": op, "path": "", "value": replacement}])
    assert result == {"views": [{"title": "new"}]}
    result["views"][0]["title"] = "mutated"
    assert replacement == {"views": [{"title": "new"}]}
    assert config == {"title": "old"}


def test_root_test_is_supported(apply_patch) -> None:
    assert apply_patch({"views": []}, [{"op": "test", "path": "", "value": {"views": []}}]) == {"views": []}


@pytest.mark.parametrize("patch", [[{"op": "remove", "path": ""}], [{"op": "replace", "path": "", "value": []}], [{"op": "add", "path": "", "value": None}]])
def test_result_must_remain_a_dictionary(apply_patch, patch) -> None:
    with pytest.raises(ValueError):
        apply_patch({"views": []}, patch)


@pytest.mark.parametrize("config", [None, [], "text", 1])
def test_input_must_be_a_dictionary(apply_patch, config) -> None:
    with pytest.raises(ValueError):
        apply_patch(config, [])


@pytest.mark.parametrize("patch", [None, {}, "[]", [None], [[]], [{}], [{"op": "add", "path": "/value"}], [{"op": "test", "path": "/value"}], [{"op": "replace", "path": "/value"}], [{"path": "/value", "value": 1}], [{"op": "copy", "path": "/value", "from": "/title"}], [{"op": [], "path": "/value"}]])
def test_malformed_patch_is_rejected(apply_patch, patch) -> None:
    with pytest.raises(ValueError):
        apply_patch({"value": 1}, patch)


def test_empty_patch_is_an_independent_copy(apply_patch) -> None:
    config = {"views": [{"cards": []}]}
    result = apply_patch(config, [])
    assert result == config
    result["views"][0]["cards"].append("new")
    assert config == {"views": [{"cards": []}]}


def test_operation_count_limit_accepts_100_and_rejects_101(apply_patch) -> None:
    operation = {"op": "add", "path": "/cards/-", "value": "card"}
    assert apply_patch({"cards": []}, [operation] * 100) == {"cards": ["card"] * 100}
    config = {"cards": []}
    with pytest.raises(ValueError):
        apply_patch(config, [operation] * 101)
    assert config == {"cards": []}


def test_unicode_templates_and_escape_sequences_are_preserved(apply_patch) -> None:
    literal = "室温 🌡️ {{ states('sensor.temp') }}\n[[[ return entity.state; ]]]\\n"
    assert apply_patch({}, [{"op": "add", "path": "/説明", "value": literal}]) == {"説明": literal}


def test_failure_does_not_echo_configuration_values(apply_patch) -> None:
    with pytest.raises(ValueError) as error:
        apply_patch({"token": "private-dashboard-secret"}, [{"op": "test", "path": "/token", "value": "private-patch-secret"}])
    assert "private-dashboard-secret" not in str(error.value)
    assert "private-patch-secret" not in str(error.value)
