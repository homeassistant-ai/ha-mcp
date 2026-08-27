import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_traditional_chinese_locale_is_available_in_settings_ui() -> None:
    locale_path = (
        REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales" / "zh-Hant.json"
    )

    catalog = json.loads(locale_path.read_text(encoding="utf-8"))

    assert catalog["meta"] == {"native_name": "繁體中文", "dir": "ltr"}


def test_traditional_chinese_tool_descriptions_are_complete_and_safe() -> None:
    catalog = json.loads(
        (
            REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales" / "zh-Hant.json"
        ).read_text(encoding="utf-8")
    )

    assert catalog["tools"]["ha_get_app"]["description"].endswith(
        "取得單一 App 的詳細資料。"
    )
    assert catalog["tools"]["ha_manage_app"]["description"].endswith(
        "代理呼叫 App API。"
    )
    assert catalog["tools"]["ha_manage_hacs"]["description"].endswith(
        "重新整理儲存庫資訊。"
    )
    assert catalog["tools"]["ha_reload_core"] == {
        "title": "重新載入核心元件",
        "description": "不必完整重新啟動，即可重新載入 Home Assistant 設定。",
    }

    messages = catalog["messages"]
    assert messages["features.redact_secrets.label"] == "遮蔽機密資訊"
    assert messages["tools.states.security_gated"] == "已套用安全閘門"
    for key in (
        "policies.global.manage_tool.warning",
        "addon.enable_security_policy_tool.description",
    ):
        assert "管理安全策略" in messages[key]
        assert "已套用安全閘門" in messages[key]
        assert "Manage Security Policy" not in messages[key]
        assert "security gated" not in messages[key]


def test_traditional_chinese_uses_the_product_app_name() -> None:
    catalog_text = (
        REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales" / "zh-Hant.json"
    ).read_text(encoding="utf-8")

    assert "應用（外掛）" not in catalog_text
    assert "應用（add-on）" not in catalog_text
    assert "App（add-on）" not in catalog_text
    assert "重新啟動應用" not in catalog_text
    assert "重新啟動App" not in catalog_text
    assert "App（附加元件）" in catalog_text


def test_traditional_chinese_memory_limit_names_the_input_unit() -> None:
    catalog = json.loads(
        (
            REPO_ROOT / "src" / "ha_mcp" / "settings_ui" / "locales" / "zh-Hant.json"
        ).read_text(encoding="utf-8")
    )

    help_text = catalog["messages"]["advanced.code_mode_max_memory.help"]
    assert "輸入值的單位是位元組" in help_text
    assert "1–256 MB" in help_text
