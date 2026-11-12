"""The click-to-restart Repairs card: when it appears, what its Submit does,
and the ``refresh_repairs`` service the app raises it through."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_mock_service
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._proxy import OAUTH_ISSUE, restart_issue, set_up_proxy, start_proxy
from ._relay import UPDATE_ISSUE, WEBHOOK_ID, restart_marker
from ._variants import Variant

pytestmark = pytest.mark.usefixtures("component")

FIX_FLOW = "/api/repairs/issues/fix"


async def refresh_repairs(
    hass: HomeAssistant, variant: Variant, issue_id: str, action: str
) -> None:
    """What the app calls through Core's REST API."""
    await hass.services.async_call(
        variant.domain,
        "refresh_repairs",
        {"issue_id": issue_id, "action": action},
        blocking=True,
    )


async def test_restart_marker_at_boot_raises_a_fixable_repair(
    hass: HomeAssistant, variant: Variant, fs_root: Path
) -> None:
    """A restart the app asked for before Home Assistant went down is still
    pending at boot: the user gets the card with its Submit button."""
    restart_marker(fs_root, variant).write_text('{"reason": "oauth_enabled"}')

    await set_up_proxy(hass, variant)

    issue = restart_issue(hass, variant)
    assert issue is not None
    assert issue.is_fixable


async def test_boot_without_marker_raises_no_repair(
    hass: HomeAssistant, variant: Variant
) -> None:
    await set_up_proxy(hass, variant)

    assert restart_issue(hass, variant) is None


async def test_clean_setup_clears_a_stale_marker_and_its_repair(
    hass: HomeAssistant, variant: Variant, fs_root: Path
) -> None:
    """Once the proxy sets up with nothing waiting for a restart, the card
    and its marker go, so the card does not come back on the next boot."""
    marker = restart_marker(fs_root, variant)
    marker.write_text('{"reason": "oauth_enabled"}')
    config = {
        "target_url": "http://127.0.0.1:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
        "webhook_id": WEBHOOK_ID,
    }

    await start_proxy(hass, variant, fs_root, config)

    assert not marker.exists()
    assert restart_issue(hass, variant) is None


async def test_clear_issue_drops_the_card_and_its_marker(
    hass: HomeAssistant, variant: Variant, fs_root: Path, component: ModuleType
) -> None:
    marker = restart_marker(fs_root, variant)
    marker.write_text("{}")
    await set_up_proxy(hass, variant)

    component.repairs.clear_issue(hass, variant.domain)

    assert not marker.exists()
    assert restart_issue(hass, variant) is None


async def open_restart_card(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
) -> dict[str, Any]:
    """Raise the OAuth restart card at boot and open it as the frontend does."""
    restart_marker(fs_root, variant).write_text("{}")
    assert await async_setup_component(hass, "repairs", {})
    await set_up_proxy(hass, variant)
    client = await hass_client()
    response = await client.post(
        FIX_FLOW, json={"handler": variant.domain, "issue_id": OAUTH_ISSUE}
    )
    assert response.status == 200
    flow: dict[str, Any] = await response.json()
    return flow


async def test_restart_card_asks_for_confirmation(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
) -> None:
    flow = await open_restart_card(hass, hass_client, variant, fs_root)

    assert flow["type"] == "form"
    assert flow["step_id"] == "confirm"


async def test_submitting_the_card_restarts_and_keeps_the_marker(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
) -> None:
    """Submit restarts Home Assistant. The marker stays: if the restart
    aborts, the card has to come back; a successful restart's setup clears
    it once the change is live."""
    restarts = async_mock_service(hass, "homeassistant", "restart")
    flow = await open_restart_card(hass, hass_client, variant, fs_root)
    client = await hass_client()

    response = await client.post(f"{FIX_FLOW}/{flow['flow_id']}", json={})

    assert response.status == 200
    assert (await response.json())["type"] == "create_entry"
    assert len(restarts) == 1
    assert restart_marker(fs_root, variant).exists()


async def test_failed_restart_is_reported_and_the_card_stays(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A restart Core refuses (say, a failed config check) must reach the
    user rather than look like it worked."""
    async_mock_service(
        hass,
        "homeassistant",
        "restart",
        raise_exception=HomeAssistantError("configuration check failed"),
    )
    flow = await open_restart_card(hass, hass_client, variant, fs_root)
    client = await hass_client()

    response = await client.post(f"{FIX_FLOW}/{flow['flow_id']}", json={})

    assert response.status != 200
    assert restart_issue(hass, variant) is not None
    assert restart_marker(fs_root, variant).exists()


async def test_both_restart_cards_render_localized_text(
    hass: HomeAssistant, variant: Variant, fs_root: Path
) -> None:
    """A custom integration's Repairs text comes only from its runtime
    translations; a missing key shows the user a raw key instead."""
    restart_marker(fs_root, variant).write_text("{}")
    await set_up_proxy(hass, variant)
    await refresh_repairs(hass, variant, UPDATE_ISSUE, "create")
    issues = [restart_issue(hass, variant, i) for i in (OAUTH_ISSUE, UPDATE_ISSUE)]

    strings = await async_get_translations(hass, "en", "issues", [variant.domain])

    for issue in issues:
        assert issue is not None
        prefix = f"component.{variant.domain}.issues.{issue.translation_key}"
        assert strings.get(f"{prefix}.title")
        assert strings.get(f"{prefix}.fix_flow.step.confirm.title")
        assert strings.get(f"{prefix}.fix_flow.step.confirm.description")


def test_runtime_translations_match_strings_json(variant: Variant) -> None:
    """``strings.json`` is the source the translation tooling reads, and
    ``translations/en.json`` is what Home Assistant loads; they must agree."""
    strings = json.loads((variant.component_dir / "strings.json").read_text())
    runtime = variant.component_dir / "translations" / "en.json"

    assert json.loads(runtime.read_text()) == strings


async def test_boot_dismisses_the_apps_restart_notification(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, variant: Variant
) -> None:
    """The app posts "integration updated, restart Home Assistant"; once the
    new code is running, that notification is stale and goes away."""
    notification_id = f"{variant.component}_update"
    assert f'"{notification_id}"' in variant.start_src()
    assert await async_setup_component(hass, persistent_notification.DOMAIN, {})
    persistent_notification.async_create(
        hass, "Restart Home Assistant", notification_id=notification_id
    )

    await set_up_proxy(hass, variant)

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "persistent_notification/get"})
    reply = await client.receive_json()
    assert notification_id not in [n["notification_id"] for n in reply["result"]]


async def test_app_raises_the_update_card_immediately(
    hass: HomeAssistant, variant: Variant
) -> None:
    """Integration files changed on disk: the card appears now, not after the
    restart it asks for."""
    await set_up_proxy(hass, variant)

    await refresh_repairs(hass, variant, UPDATE_ISSUE, "create")

    issue = restart_issue(hass, variant, UPDATE_ISSUE)
    assert issue is not None
    assert issue.is_fixable


async def test_app_raises_the_oauth_card_only_with_its_marker(
    hass: HomeAssistant, variant: Variant, fs_root: Path
) -> None:
    """The marker is what keeps the OAuth card across an aborted restart, so
    a card without it would vanish at the next boot."""
    await set_up_proxy(hass, variant)

    await refresh_repairs(hass, variant, OAUTH_ISSUE, "create")
    assert restart_issue(hass, variant, OAUTH_ISSUE) is None

    restart_marker(fs_root, variant).write_text("{}")
    await refresh_repairs(hass, variant, OAUTH_ISSUE, "create")
    assert restart_issue(hass, variant, OAUTH_ISSUE) is not None


async def test_app_clears_a_card_it_raised(
    hass: HomeAssistant, variant: Variant
) -> None:
    await set_up_proxy(hass, variant)
    await refresh_repairs(hass, variant, UPDATE_ISSUE, "create")

    await refresh_repairs(hass, variant, UPDATE_ISSUE, "clear")

    assert restart_issue(hass, variant, UPDATE_ISSUE) is None
