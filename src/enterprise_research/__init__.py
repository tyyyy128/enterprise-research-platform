"""Enterprise Research Platform DeerFlow extension."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deerflow_extension_api import ExtensionInstall, ExtensionRegistry, extension

from enterprise_research.plugin import EnterpriseResearchService, build_router

__all__ = ["install"]


@extension(api="0.2.0", name="enterprise-research")
def install(
    registry: ExtensionRegistry,
    config: Mapping[str, Any],
) -> None:
    """Register the Enterprise Research extension."""

    if config.get("enabled", True) is False:
        return

    service = EnterpriseResearchService()

    registry.service(service)
    registry.routers((build_router(service),))


_entry_point: ExtensionInstall = install
