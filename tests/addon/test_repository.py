"""Test Home Assistant add-on repository configuration."""

import os
import yaml


class TestRepository:
    """Verify repository.yaml meets Home Assistant requirements."""

    def test_repository_yaml_exists(self) -> None:
        """Check repository.yaml exists at project root."""
        assert os.path.exists("repository.yaml"), "repository.yaml is required for HA add-on repository"

    def test_repository_yaml_valid(self) -> None:
        """Verify repository.yaml is valid YAML with required fields."""
        with open("repository.yaml") as f:
            config = yaml.safe_load(f)

        # Verify required field
        assert "name" in config, "repository.yaml must have 'name' field"
        assert config["name"], "name field cannot be empty"

        # Verify optional but recommended fields
        assert "url" in config, "repository.yaml should have 'url' field"
        assert "maintainer" in config, "repository.yaml should have 'maintainer' field"

    def test_repository_matches_addon(self) -> None:
        """Verify repository.yaml matches add-on config.yaml."""
        with open("repository.yaml") as f:
            repo_config = yaml.safe_load(f)

        with open("homeassistant-addon/config.yaml") as f:
            addon_config = yaml.safe_load(f)

        # URL should match
        assert repo_config["url"] == addon_config["url"], \
            "repository.yaml and config.yaml URLs should match"
