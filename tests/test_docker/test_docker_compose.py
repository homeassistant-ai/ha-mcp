"""Test docker-compose.yml and related configuration."""

import os
import re

import yaml

DATA_DIR = "/home/mcpuser/.ha-mcp"


class TestDockerCompose:
    """Validate docker-compose configuration."""

    def test_docker_compose_valid_yaml(self):
        """Verify docker-compose.yml is valid YAML."""
        with open("docker-compose.yml") as f:
            compose = yaml.safe_load(f)
        assert "services" in compose
        assert "ha-mcp" in compose["services"]

    def test_ha_mcp_service_configuration(self):
        """Verify ha-mcp service has required configuration."""
        with open("docker-compose.yml") as f:
            compose = yaml.safe_load(f)

        ha_mcp = compose["services"]["ha-mcp"]
        assert "build" in ha_mcp or "image" in ha_mcp
        assert "environment" in ha_mcp

        env = ha_mcp["environment"]
        env_vars = {item.split("=")[0]: item for item in env}
        assert "HOMEASSISTANT_URL" in env_vars
        assert "HOMEASSISTANT_TOKEN" in env_vars

    def test_data_dir_is_persisted_by_a_named_volume(self):
        """Verify the example compose keeps ``~/.ha-mcp`` on a named volume.

        Without it the settings UI's "restart for changes to take effect"
        advice destroys the very changes it asks you to apply, because
        re-creating the container discards the writable layer — issue #2078.
        """
        with open("docker-compose.yml") as f:
            compose = yaml.safe_load(f)

        mounts = compose["services"]["ha-mcp"]["volumes"]
        named = [m for m in mounts if m.endswith(f":{DATA_DIR}")]
        assert named, f"no volume mounted at {DATA_DIR}: {mounts}"

        # The example is expected to use a *named* volume specifically, and a
        # named volume has to be declared top-level too or `docker compose up`
        # rejects the file outright. A bind mount would fail this assert by
        # design — swapping one in would mean rewriting the docs to match.
        volume_name = named[0].split(":")[0]
        declared = compose.get("volumes", {})
        assert volume_name in declared

        # Compose scopes a named volume to the project (<project>_ha-mcp-data)
        # unless `name:` pins it. Every `docker run` recipe, the OAuth
        # persistence-failure hint and the `docker volume rm ha-mcp-data`
        # recovery step name it unprefixed, so the pin is what keeps compose
        # and non-compose users on the same volume.
        spec = declared[volume_name] or {}
        assert spec.get("name") == volume_name, (
            f"{volume_name} needs an explicit `name:`, or Compose creates "
            f"<project>_{volume_name} and the documented recovery command "
            "targets a volume that doesn't exist"
        )

    def test_documented_compose_snippets_pin_the_volume_name(self):
        """Verify the compose snippets on the docs site pin ``name:`` too.

        These are the blocks users actually copy, and each page also tells
        them to recover with ``docker volume rm ha-mcp-data``. A bare
        ``ha-mcp-data:`` declaration makes Compose create
        ``<project>_ha-mcp-data``, so that command would target a volume
        they don't have — in the FAQ the two sit one paragraph apart.
        Checking only ``docker-compose.yml`` would miss exactly the copies
        the pin was introduced to protect.
        """
        pages = [
            "site/src/pages/setup.astro",
            "site/src/pages/faq.astro",
        ]
        # Keyed by page, not a running total: counting qualifying blocks would
        # let two declarations on one page cover a page that has none.
        checked: set[str] = set()
        for page in pages:
            with open(page, encoding="utf-8") as f:
                source = f.read()

            # Each fenced block is `<code>services:` ... `</code>`. Astro
            # template literals escape `$`, which YAML does not care about.
            for block in re.findall(r"<code>(services:.*?)</code>", source, re.DOTALL):
                snippet = yaml.safe_load(block.replace("\\${", "${"))
                declared = snippet.get("volumes")
                if not declared or "ha-mcp-data" not in declared:
                    continue  # service-only fragment, nothing to pin
                checked.add(page)
                spec = declared["ha-mcp-data"] or {}
                assert spec.get("name") == "ha-mcp-data", (
                    f"{page}: compose snippet declares a bare ha-mcp-data, so "
                    "Compose would create <project>_ha-mcp-data and the "
                    "documented `docker volume rm ha-mcp-data` cannot match it"
                )

        assert checked == set(pages), (
            "expected a top-level volumes declaration on every page; missing "
            f"{sorted(set(pages) - checked)} — did a snippet move or stop parsing?"
        )

    def test_screenshot_overlay_does_not_claim_the_data_dir(self):
        """Verify the screenshot overlay leaves persistence to the base file.

        Compose merges a service's ``volumes`` by container path, so a mount
        at the data dir here would REPLACE whatever the base file mounts
        there. Operators keeping settings on a custom host path would have
        their writes silently redirected to a different volume the moment
        they enabled the screenshot overlay.
        """
        with open("docker-compose.screenshot.yml") as f:
            overlay = yaml.safe_load(f)

        mounts = overlay["services"]["ha-mcp"].get("volumes", [])
        clobbering = [m for m in mounts if m.split(":")[1:2] == [DATA_DIR]]
        assert not clobbering, (
            f"overlay mounts {clobbering} at {DATA_DIR}, overriding the base "
            "file's persistence volume"
        )

    def test_runtime_volume_hint_matches_the_documented_name(self):
        """Verify the OAuth failure hint names the volume the docs tell you to create.

        ``provider.py`` tells operators to mount a volume when it can't
        persist the client registry. Docker treats ``ha-mcp-data`` and
        ``ha_mcp_data`` as two different volumes, so a hint that disagrees
        with the documented recipe sends people to a fresh, empty volume and
        looks like the data was lost.
        """
        with open("docker-compose.yml") as f:
            compose = yaml.safe_load(f)
        documented = next(
            m.split(":")[0]
            for m in compose["services"]["ha-mcp"]["volumes"]
            if m.endswith(f":{DATA_DIR}")
        )

        with open("src/ha_mcp/auth/provider.py", encoding="utf-8") as f:
            source = f.read()
        hints = re.findall(rf"docker run -v (\S+):{re.escape(DATA_DIR)}", source)
        assert hints, "provider.py no longer suggests mounting a volume"
        assert all(h == documented for h in hints), (
            f"provider.py suggests {hints}, docs use {documented!r}"
        )

    def test_dockerignore_exists(self):
        """Verify .dockerignore exists to optimize builds."""
        assert os.path.exists(".dockerignore")

        with open(".dockerignore") as f:
            content = f.read()

        # Should exclude development files
        assert "tests/" in content
        assert ".git" in content
        assert "__pycache__" in content
