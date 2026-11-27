"""Seeding of the fresh Home Assistant config directory before the container boots."""

import importlib.util
import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _setup_config_permissions(config_path: Path) -> None:
    """Set up proper permissions for Home Assistant config directory."""
    import stat

    # Set directory permissions recursively
    for root, dirs, files in os.walk(config_path):
        for d in dirs:
            os.chmod(
                os.path.join(root, d),
                stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH,
            )
        for f in files:
            os.chmod(
                os.path.join(root, f),
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IRGRP
                | stat.S_IWGRP
                | stat.S_IROTH,
            )


def _install_custom_component(
    config_path: Path,
    component_src: Path,
    domain: str,
    title: str,
    *,
    seed_entry: bool = True,
) -> bool:
    """Install a custom component into the test HA config.

    Copies component source into custom_components/<domain> and injects a
    config entry so HA loads it on startup. Returns True if installed.

    ``seed_entry=False`` copies the files but seeds NO config entry — the
    no-tools lanes (#2292) need ha_mcp_tools' code present (the in-process
    server entry loads from it) with its File & YAML Tools entry absent, so
    the privileged services never register.
    """
    if not component_src.exists():
        logger.info("%s source not found — skipping installation", domain)
        return False

    dest = config_path / "custom_components" / domain
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(component_src, dest, dirs_exist_ok=True)

    # Inject config entry if not already present
    storage_file = config_path / ".storage" / "core.config_entries"
    if seed_entry and storage_file.exists():
        data = json.loads(storage_file.read_text())
        entries = data.get("data", {}).get("entries", [])
        if not any(isinstance(e, dict) and e.get("domain") == domain for e in entries):
            entries.append(
                {
                    "created_at": "2025-09-07T23:56:28.040744+00:00",
                    "data": {},
                    "disabled_by": None,
                    "discovery_keys": {},
                    "domain": domain,
                    "entry_id": f"e2e_test_{domain}_entry",
                    "minor_version": 1,
                    "modified_at": "2025-09-07T23:56:28.040747+00:00",
                    "options": {},
                    "pref_disable_new_entities": False,
                    "pref_disable_polling": False,
                    "source": "import",
                    "subentries": [],
                    "title": title,
                    "unique_id": domain,
                    "version": 1,
                }
            )
            storage_file.write_text(json.dumps(data, indent=2))

    logger.info(
        "Installed %s component%s", domain, "" if seed_entry else " (no config entry)"
    )
    return True


def _seed_legacy_yaml_backups(config_path: Path) -> None:
    """Stage pre-#1579 legacy ``.bak`` artifacts before the container boots (#1579).

    The component's ``list_legacy_backups`` / ``read_legacy_backup`` services read
    ``<config>/.ha_mcp_tools_backups/`` live, but a post-boot host write to the
    bind-mounted config dir doesn't propagate in CI — so the legacy-backup e2e
    seeds here, at the same pre-boot stage the rest of the test config is laid
    down (``_setup_config_permissions`` then runs over it like everything else).

    Two fixed artifacts:
    - ``themes_e2elegacy.yaml.<ts>.bak`` decodes unambiguously to
      ``themes/e2elegacy.yaml`` (no underscore in the basename), so restore can
      target it.
    - ``packages_foo_bar.yaml.<ts>.bak`` has a literal underscore, making
      ``packages/foo_bar.yaml`` vs ``packages/foo/bar.yaml`` indistinguishable,
      so restore must refuse rather than guess.
    """
    legacy_dir = config_path / ".ha_mcp_tools_backups"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "themes_e2elegacy.yaml.20200101_000000.bak").write_text(
        "e2elegacy:\n  primary-color: '#abcdef'\n"
    )
    (legacy_dir / "packages_foo_bar.yaml.20200101_000000.bak").write_text(
        "# legacy ambiguous artifact\nswitch: []\n"
    )


def _seed_non_yaml_package_file(config_path: Path) -> None:
    """Stage a non-YAML file inside the bound packages folder pre-boot (#1788).

    ``ha_config_get_yaml``'s glob is not restricted to ``*.yaml``, so
    ``custom_packages/*`` legitimately turns up a file the component refuses to
    read (its package-dir read rule requires ``.yaml``). The warn-and-continue
    path that keeps one such file from sinking the whole search needs the real
    component to produce that refusal, and no tool can create the file:
    ``write_file`` is never granted package access, so it is staged here, like
    the legacy backups above (a post-boot host write doesn't propagate in CI).

    Inert at boot: HA loads a ``!include_dir_named`` folder through
    ``_find_files(loc, "*.yaml")`` (annotatedyaml), so a ``.md`` is ignored.
    The folder name matches the one initial_test_state/configuration.yaml binds.
    """
    packages_dir = config_path / "custom_packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    (packages_dir / "_e2e_not_yaml.md").write_text(
        "Not YAML. Staged so a packages glob has a file the component skips.\n"
    )


# scene_id / entity_id the YAML-package scene below lands at. HA slugifies the
# ``name`` into the entity_id while the declared ``id`` becomes the registry
# unique_id / storage key. They are deliberately DIFFERENT here so the e2e can
# discriminate the registry-hit classification branch (see the helper below).
E2E_YAML_PACKAGE_SCENE_ENTITY_ID = (
    "e2e_yaml_scene_1971"  # HA slugifies the name to this
)
E2E_YAML_PACKAGE_SCENE_UID = (
    "e2e_yaml_scene_1971_storage_uid"  # declared id / storage key
)
# Sibling scene declaring NO ``id``. HA core makes ``id`` optional and maps it
# straight to the entity's unique_id (homeassistant/components/homeassistant/
# scene.py: ``unique_id`` -> ``scene_config.id``), so this one gets no registry
# entry at all while still living in the state machine under its name slug -
# the registry-MISS arm of the same not-storage-scene case.
E2E_YAML_PACKAGE_SCENE_IDLESS_ENTITY_ID = "e2e_yaml_scene_1971_idless"


def _seed_yaml_package_scene(config_path: Path) -> None:
    """Stage a YAML-package scene declaring an ``id`` pre-boot (#1971).

    A scene defined in a YAML package with an ``id`` registers in the entity
    registry (unique_id = that ``id``) yet has NO entry in the managed
    ``scenes.yaml`` store, so ``config/scene/config/{id}`` 404s. That is the
    exact registry-hit not-storage-scene case #1971 classifies as
    CONFIG_NOT_FOUND. It is also the only arm of that case that can run
    in-container (the Hue/vendor arm needs a real integration). No tool can
    create it: scene writes go through the managed store, and a post-boot host
    write to the bind-mounted config dir doesn't propagate in CI, so it is
    staged here like the legacy backups above. The folder name matches the one
    initial_test_state/configuration.yaml binds via ``!include_dir_named``.

    The declared ``id`` is deliberately NOT the slug of ``name``: the caller
    references the scene by its name-derived entity slug, which resolves in the
    registry to this distinct ``id`` (registry_hit). So a regression that
    dropped the registry-hit classification branch would fall through to the
    state-machine check on the storage key ``id`` (which is NOT a real entity,
    since the entity is the name slug), yielding the generic 404 and failing the
    e2e. That makes the test pin the registry-hit path, not just the state-check
    fallback.

    A second scene in the same package declares NO ``id``. It therefore has no
    registry entry at all (registry MISS) yet still exists in the state machine,
    which is the other arm the classification has to cover: on the read path via
    the state check, and on the no-hash write path so a plain ``set`` cannot
    shadow-create over it either.

    The filename must NOT start with an underscore: ``!include_dir_named`` uses
    the file stem as the package name, and ``PACKAGES_CONFIG_SCHEMA`` validates
    each package name with ``cv.slug``, which rejects a leading underscore
    (slugify strips it, so ``value != slugify(value)`` raises). A ``_``-prefixed
    name makes HA discard the whole package and the scene silently never loads.
    """
    packages_dir = config_path / "custom_packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    (packages_dir / "e2e_yaml_scene.yaml").write_text(
        "scene:\n"
        f"  - id: {E2E_YAML_PACKAGE_SCENE_UID}\n"
        "    name: E2E YAML Scene 1971\n"
        "    entities:\n"
        "      light.bed_light:\n"
        '        state: "on"\n'
        "  - name: E2E YAML Scene 1971 Idless\n"
        "    entities:\n"
        "      light.bed_light:\n"
        '        state: "on"\n'
    )


# The component's own server-pin rule, loaded by path: importing the package
# would pull in Home Assistant.
_PIN_RULE_SPEC = importlib.util.spec_from_file_location(
    "server_pin",
    Path(__file__).resolve().parents[3]
    / "custom_components"
    / "ha_mcp_tools"
    / "server_pin.py",
)
assert _PIN_RULE_SPEC is not None and _PIN_RULE_SPEC.loader is not None
_server_pin = importlib.util.module_from_spec(_PIN_RULE_SPEC)
_PIN_RULE_SPEC.loader.exec_module(_server_pin)


def is_server_requirement(requirement: str) -> bool:
    """True for the ha-mcp server pin the component manifest carries (#2427).

    On the embedded lanes the checkout's own wheel supplies the server, so
    installing the pinned PyPI release first would put released code under
    test (pip then treats the same-version wheel as already satisfied).
    """
    return _server_pin.server_dist(requirement) is not None


def _collect_manifest_requirements(
    config_path: Path, *, skip_server: bool = False
) -> list[str]:
    """Aggregate ``requirements`` from every installed custom-component manifest.

    Returns a de-duplicated ordered list of pip-installable requirement
    strings (e.g. ``["ruamel.yaml>=0.18.0"]``).

    Used to pre-install third-party packages in the HA container's Python
    env before HA boots: HA's runtime manifest-requirement-install does
    not reliably fire for config entries that the e2e fixture pre-injects
    via ``.storage/core.config_entries`` (the path
    ``_install_custom_component`` takes), so an integration that imports
    a third-party package at module load or in ``async_setup_entry``
    would otherwise hit ``ModuleNotFoundError`` and end up in
    ``state=setup_error``. Live evidence on PR #1268 ARM E2E
    (2026-05-12): ``ruamel.yaml`` was never installed by HA on that run.
    """
    cc_dir = config_path / "custom_components"
    if not cc_dir.exists():
        return []
    reqs: list[str] = []
    for manifest_path in sorted(cc_dir.glob("*/manifest.json")):
        try:
            manifest_data = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                f"⚠️ Could not read {manifest_path}: {type(exc).__name__}: {exc}"
            )
            continue
        for req in manifest_data.get("requirements", []):
            if not isinstance(req, str) or req in reqs:
                continue
            if skip_server and is_server_requirement(req):
                continue
            reqs.append(req)
    return reqs
