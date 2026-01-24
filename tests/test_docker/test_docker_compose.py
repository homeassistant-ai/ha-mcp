"""Test docker-compose.yml and related configuration."""

import os
import yaml


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

    def test_dockerignore_exists(self):
        """Verify .dockerignore exists to optimize builds."""
        assert os.path.exists(".dockerignore")

        with open(".dockerignore") as f:
            content = f.read()

        # Should exclude development files
        assert "tests/" in content
        assert ".git" in content
        assert "__pycache__" in content
