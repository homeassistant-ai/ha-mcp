"""FastMCP server implementation of the MCP Skills extension."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from ha_mcp._vendor.mcp.server.context import ServerRequestContext
from ha_mcp._vendor.mcp.shared.exceptions import MCPError
from ha_mcp._vendor.mcp_types import INVALID_PARAMS
from ha_mcp._vendor.mcp_types.version import MODERN_PROTOCOL_VERSIONS

from ha_mcp._vendor.fastmcp.server.context import Context
from ha_mcp._vendor.fastmcp.server.extensions import MethodBinding, ServerExtension
from ha_mcp._vendor.fastmcp.server.mixins.mcp_operations import _apply_pagination
from ha_mcp._vendor.fastmcp.server.providers.base import Provider
from ha_mcp._vendor.fastmcp.skills._constants import SKILLS_EXTENSION_ID
from ha_mcp._vendor.fastmcp.skills._source import _SkillSource
from ha_mcp._vendor.fastmcp.skills.models import (
    GetSkillParams,
    GetSkillResult,
    ListSkillsParams,
    ListSkillsResult,
)
from ha_mcp._vendor.fastmcp.utilities.async_utils import gather
from ha_mcp._vendor.fastmcp.utilities.logging import get_logger

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.server.server import FastMCP

logger = get_logger(__name__.removeprefix("ha_mcp._vendor."))

_SKILLS_METHOD_VERSIONS = frozenset(MODERN_PROTOCOL_VERSIONS)


class SkillsExtension(ServerExtension):
    """Expose caller-scoped skill sources through SEP-2640 methods."""

    identifier = SKILLS_EXTENSION_ID

    def __init__(self, *, providers: Sequence[Provider]) -> None:
        if not providers:
            raise ValueError("SkillsExtension requires at least one provider")
        if len({id(provider) for provider in providers}) != len(providers):
            raise ValueError("SkillsExtension providers must be unique")
        self._providers = tuple(providers)

    def _bind(self, server: FastMCP) -> None:
        self._validate_configuration(server)
        super()._bind(server)

    def _validate_configuration(self, server: FastMCP) -> None:
        if server.transforms:
            raise ValueError(
                "SkillsExtension does not yet support server-level transforms"
            )

        for provider in self._providers:
            if not any(registered is provider for registered in server.providers):
                raise ValueError(
                    "SkillsExtension providers must be registered directly on the "
                    "same FastMCP server before the extension"
                )
            if provider.transforms:
                raise ValueError(
                    "SkillsExtension does not yet support transformed providers"
                )
            if not isinstance(provider, _SkillSource):
                raise TypeError(
                    f"{type(provider).__name__} is not a SkillsExtension source"
                )

            provider._validate_skill_source()

    @property
    def _sources(self) -> tuple[_SkillSource, ...]:
        return cast("tuple[_SkillSource, ...]", self._providers)

    def _requires_live_resource(self, uri: str) -> bool:
        """Keep manifest-backed resources coherent with current file bytes."""
        return any(source._owns_skill_resource(uri) for source in self._sources)

    def methods(self) -> Sequence[MethodBinding]:
        return (
            MethodBinding(
                method="skills/list",
                params_type=ListSkillsParams,
                handler=self._handle_list,
                protocol_versions=_SKILLS_METHOD_VERSIONS,
            ),
            MethodBinding(
                method="skills/get",
                params_type=GetSkillParams,
                handler=self._handle_get,
                protocol_versions=_SKILLS_METHOD_VERSIONS,
            ),
        )

    def _validate_live_configuration(self) -> None:
        self._validate_configuration(self.server)

    async def _handle_list(
        self,
        context: ServerRequestContext[Any, Any],
        params: ListSkillsParams,
    ) -> ListSkillsResult:
        self._validate_live_configuration()

        async with Context(fastmcp=self.server):
            source_entries = await gather(
                source._list_skill_entries(context) for source in self._sources
            )
            identities = await gather(source._skill_uris() for source in self._sources)
        entries = [entry for group in source_entries for entry in group]
        counts = Counter(uri for group in identities for uri in group)
        collisions = sorted(uri for uri, count in counts.items() if count > 1)
        if collisions:
            logger.warning(
                "Omitting duplicate Skills entries for URIs: %s",
                ", ".join(collisions),
            )
        entries = sorted(
            (entry for entry in entries if counts[entry.uri] == 1),
            key=lambda entry: entry.uri,
        )
        page, next_cursor = _apply_pagination(
            entries, params.cursor, self.server._list_page_size
        )
        return ListSkillsResult(skills=page, next_cursor=next_cursor)

    async def _handle_get(
        self,
        context: ServerRequestContext[Any, Any],
        params: GetSkillParams,
    ) -> GetSkillResult:
        self._validate_live_configuration()

        async with Context(fastmcp=self.server):
            identities = await gather(
                source._skill_uris(params.uri) for source in self._sources
            )
            if sum(group.count(params.uri) for group in identities) != 1:
                raise MCPError(
                    code=INVALID_PARAMS, message=f"Unknown skill URI: {params.uri}"
                )
            results = await gather(
                source._get_skill_entry(params.uri, context) for source in self._sources
            )
        matches = [entry for entry in results if entry is not None]
        if len(matches) != 1 or matches[0].uri != params.uri:
            raise MCPError(
                code=INVALID_PARAMS,
                message=f"Unknown skill URI: {params.uri}",
            )
        return GetSkillResult(skill=matches[0])
