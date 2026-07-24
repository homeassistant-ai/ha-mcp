"""End-to-end coverage for the not-storage-scene classification (issue #1971).

A scene defined in a YAML package with an ``id`` registers in the entity
registry (unique_id = that ``id``) yet has NO entry in the managed
``scenes.yaml`` store, so ``config/scene/config/{id}`` 404s. That is the exact
registry-hit case #1971 must classify as ``CONFIG_NOT_FOUND`` rather than the
misleading ``ENTITY_NOT_FOUND``, because the entity plainly exists.

Its ``id``-less sibling is the same case reached the other way: HA core makes
``id`` optional and maps it straight to the entity's unique_id, so without one
there is no registry entry to resolve, only a state-machine entity. Both arms
run here because the classification takes a different route for each.

Why an e2e test earns its keep here: the tool-layer unit tests inject
``SceneStorageConfigNotFoundError`` directly, so a wiring break between the
resolver, the config-API GET and the tool handler would pass the unit suite
while shipping the old error. This spans the whole chain through the real
component. The Hue/vendor arm and the platform-named message can't run
in-container (they need a real integration); the YAML-package arm can.

Both scenes are staged pre-boot by ``conftest._seed_yaml_package_scene`` (a
post-boot host write to the bind-mounted config dir doesn't propagate in CI),
which only runs on the testcontainer backends, so these tests skip elsewhere.
"""

import logging

import pytest

from ...conftest import (
    E2E_YAML_PACKAGE_SCENE_ENTITY_ID,
    E2E_YAML_PACKAGE_SCENE_IDLESS_ENTITY_ID,
)
from ...utilities.assertions import safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _require_seeded_backend(container_info: dict) -> None:
    """Skip unless the YAML-package scenes were staged into the container config_path.

    ``_seed_yaml_package_scene`` runs on the shared testcontainer setup path,
    which both the ``container`` and ``embedded`` backends use. The HAOS
    backends boot a pre-baked qcow2 with no such seed.
    """
    if container_info.get("backend") not in ("container", "embedded"):
        pytest.skip(
            "not-storage-scene e2e relies on the pre-boot YAML-package scene "
            "seeded in the container config_path (testcontainer / embedded only)"
        )


# The two YAML-package scenes staged by the conftest helper, each reaching the
# not-storage-scene classification by a different route: the ``id``-bearing one
# resolves in the entity registry (registry hit), the ``id``-less one is invisible
# there and is only found in the state machine (registry miss).
_SCENE_ARMS = [
    pytest.param(E2E_YAML_PACKAGE_SCENE_ENTITY_ID, id="registry-hit"),
    pytest.param(E2E_YAML_PACKAGE_SCENE_IDLESS_ENTITY_ID, id="registry-miss-idless"),
]


class TestYamlPackageSceneNotStorage:
    """A YAML-package scene surfaces CONFIG_NOT_FOUND, not a missing-entity 404."""

    @pytest.mark.parametrize("scene_id", _SCENE_ARMS)
    async def test_get_yaml_package_scene_maps_to_config_not_found(
        self, ha_container_with_fresh_config, mcp_client, scene_id
    ):
        _require_seeded_backend(ha_container_with_fresh_config)

        data = await safe_call_tool(
            mcp_client,
            "ha_config_get_scene",
            {"scene_id": scene_id},
        )

        assert data.get("success") is False, (
            f"a YAML-package scene has no editable storage config; get should "
            f"fail with CONFIG_NOT_FOUND, got: {data}"
        )
        err = data.get("error") or {}
        assert err.get("code") == "CONFIG_NOT_FOUND", err
        assert err.get("code") != "ENTITY_NOT_FOUND", err
        # The entity exists, so the message must not read as a missing entity.
        assert "editable" in (err.get("message") or "").lower(), err
        assert any("turn_on" in s for s in err.get("suggestions", [])), err

    @pytest.mark.parametrize("scene_id", _SCENE_ARMS)
    async def test_set_no_hash_yaml_package_scene_does_not_shadow_create(
        self, ha_container_with_fresh_config, mcp_client, scene_id
    ):
        """#1971 P1 end-to-end: a plain ``set`` (no config_hash) on an existing
        YAML-package scene pre-checks the config API and surfaces
        CONFIG_NOT_FOUND instead of POSTing a duplicate managed ``scenes.yaml``
        entry. Both arms matter: the pre-check opens on a registry hit OR on a
        state-machine hit, and only the latter covers the ``id``-less scene."""
        _require_seeded_backend(ha_container_with_fresh_config)

        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": {
                    "name": "Shadow Copy 1971",
                    "entities": {"light.bed_light": {"state": "off"}},
                },
                "wait": False,
            },
        )

        assert data.get("success") is False, (
            f"a plain set on a non-storage scene must not shadow-create; "
            f"expected CONFIG_NOT_FOUND, got: {data}"
        )
        err = data.get("error") or {}
        assert err.get("code") == "CONFIG_NOT_FOUND", err

        # The error alone does not prove nothing was written: read back through
        # the config API. A shadow entry in the managed store would make this
        # succeed, so the same CONFIG_NOT_FOUND is the actual no-write evidence.
        after = await safe_call_tool(
            mcp_client,
            "ha_config_get_scene",
            {"scene_id": scene_id},
        )
        assert after.get("success") is False, (
            f"the rejected set still created a managed scenes.yaml entry: {after}"
        )
        assert (after.get("error") or {}).get("code") == "CONFIG_NOT_FOUND", after
