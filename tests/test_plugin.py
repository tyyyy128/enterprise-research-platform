from __future__ import annotations

import asyncio
from typing import Any

from deerflow_extension_api import (
    ExtensionData,
    ExtensionRuntimeDeps,
    HostPolicySnapshot,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from enterprise_research import install


class FakeRegistry:
    def __init__(self) -> None:
        self.services: list[Any] = []
        self.contributed_routers: list[Any] = []

    def service(self, service: Any) -> None:
        self.services.append(service)

    def routers(self, routers: Any) -> None:
        self.contributed_routers.extend(routers)


def test_install_registers_service_and_router() -> None:
    registry = FakeRegistry()

    install(registry, {})

    assert len(registry.services) == 1
    assert len(registry.contributed_routers) == 1

    assert {route.path for route in registry.contributed_routers[0].routes} == {
        "/api/enterprise-research/health",
        "/api/enterprise-research/tasks",
        "/api/enterprise-research/tasks/{task_id}",
    }

    assert install.__deerflow_api__ == "0.2.0"
    assert install.__deerflow_name__ == "enterprise-research"


def test_disabled_extension_registers_nothing() -> None:
    registry = FakeRegistry()

    install(registry, {"enabled": False})

    assert registry.services == []
    assert registry.contributed_routers == []


def test_health_follows_service_lifecycle() -> None:
    registry = FakeRegistry()
    install(registry, {})

    service = registry.services[0]

    async def exercise() -> tuple[int, int, dict[str, Any], int]:
        app = FastAPI()
        app.include_router(registry.contributed_routers[0])

        transport = ASGITransport(app=app)

        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            before_start = await client.get("/api/enterprise-research/health")

            await service.start(
                ExtensionRuntimeDeps(
                    app_store=ExtensionData("enterprise-research"),
                    policy=HostPolicySnapshot(max_subagents_per_run=3),
                    session_factory=object(),
                )
            )

            running = await client.get("/api/enterprise-research/health")

            await service.stop()

            after_stop = await client.get("/api/enterprise-research/health")

        return (
            before_start.status_code,
            running.status_code,
            running.json(),
            after_stop.status_code,
        )

    before, running, body, after = asyncio.run(exercise())

    assert before == 503
    assert running == 200
    assert after == 503

    assert body["status"] == "ok"
    assert body["extension"] == "enterprise-research"
    assert body["scope_id"] == "enterprise-research"
    assert body["session_factory_available"] is True
