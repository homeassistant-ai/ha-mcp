import asyncio

from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config


def test_load_hidden_set_reads_config(tmp_path, monkeypatch):
    save_visibility_config(
        tmp_path, VisibilityConfig(enabled=True, exclude_categories=["diagnostic"])
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {
        "success": True,
        "result": [{"entity_id": "s.b", "entity_category": "diagnostic"}],
    }
    assert asyncio.run(resolver.load_hidden_set(reg))[0] == {"s.b"}


def test_load_hidden_set_fails_open_on_bad_config(tmp_path, monkeypatch):
    (tmp_path / "entity_visibility.json").write_text("{ corrupt")
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    reg = {
        "success": True,
        "result": [{"entity_id": "s.b", "entity_category": "diagnostic"}],
    }
    hidden, warnings = asyncio.run(resolver.load_hidden_set(reg))
    assert hidden == set()
    assert any("could not be loaded" in w for w in warnings)
