"""Unit tests for the Core-annotation YAML key finder."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _filesystem_tools_enabled(monkeypatch):
    from ha_mcp import config as ha_mcp_config
    from ha_mcp.tools.tools_filesystem import _reset_caller_token_cache

    _reset_caller_token_cache()
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    monkeypatch.setenv("HAMCP_ENABLE_FILESYSTEM_TOOLS", "true")
    monkeypatch.setattr(ha_mcp_config, "_settings", None)
    yield
    _reset_caller_token_cache()
    ha_mcp_config._settings = None


class TestFindYamlKeyFiles:
    async def test_filters_core_discovered_files_by_the_requested_glob(
        self, monkeypatch
    ):
        from ha_mcp.tools import tools_yaml_read

        async def fake_service(client, service, data):
            assert service == "find_yaml_key"
            assert data == {"yaml_path": "template"}
            return {
                "success": True,
                "files": [
                    "custom_packages/nested/mcp.yaml",
                    "other.yaml",
                ],
            }

        monkeypatch.setattr(tools_yaml_read, "call_mcp_tools_service", fake_service)

        files = await tools_yaml_read._find_yaml_key_files(
            object(), "custom_packages/*.yaml", "template"
        )

        assert files == ["custom_packages/nested/mcp.yaml"]

    async def test_non_list_files_falls_back(self, monkeypatch):
        from ha_mcp.tools import tools_yaml_read

        async def fake_service(client, service, data):
            return {"success": True, "files": "not-a-list"}

        monkeypatch.setattr(tools_yaml_read, "call_mcp_tools_service", fake_service)

        assert (
            await tools_yaml_read._find_yaml_key_files(
                object(), "custom_packages/*.yaml", "template"
            )
            is None
        )

    async def test_component_failure_falls_back(self, monkeypatch):
        from ha_mcp.tools import tools_yaml_read

        async def fake_service(client, service, data):
            raise RuntimeError("component unavailable")

        monkeypatch.setattr(tools_yaml_read, "call_mcp_tools_service", fake_service)

        assert (
            await tools_yaml_read._find_yaml_key_files(
                object(), "custom_packages/*.yaml", "template"
            )
            is None
        )


async def test_tool_uses_core_discovery_for_a_glob():
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, client = await _make_tool(
        {
            "find_yaml_key": {
                "success": True,
                "files": ["custom_packages/nested/mcp.yaml", "other.yaml"],
            },
            "read_file": _read_ok("template:\n  - trigger: []\n"),
        }
    )

    out = await fn(yaml_path="template", file="custom_packages/*.yaml")

    assert out["count"] == 1
    assert out["files_searched"] == 1
    assert out["matches"][0]["file"] == "custom_packages/nested/mcp.yaml"
    assert any(
        c.args[1] == "find_yaml_key" for c in client.call_service.await_args_list
    )


async def test_tool_falls_back_when_discovery_has_no_files():
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, client = await _make_tool(
        {
            "find_yaml_key": {"success": True, "files": []},
            "list_files": {
                "success": True,
                "files": [{"path": "custom_packages/a.yaml", "is_dir": False}],
            },
            "read_file": _read_ok("template:\n"),
        }
    )

    out = await fn(yaml_path="template", file="custom_packages/*.yaml")

    assert out["count"] == 1
    assert any(c.args[1] == "list_files" for c in client.call_service.await_args_list)
