"""The running Core's release, for tests of a path only a newer Core has."""

from ha_mcp.client.rest_client import HomeAssistantClient


async def core_at_least(
    ha_client: HomeAssistantClient, release: tuple[int, int]
) -> bool:
    """Whether the Core under test is ``release`` (year, month) or newer."""
    version = (await ha_client.get_config())["version"]
    year, month = (int(part) for part in version.split(".")[:2])
    return (year, month) >= release
