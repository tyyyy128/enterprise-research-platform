"""Async PostgreSQL database infrastructure."""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """Connection settings for Enterprise Research PostgreSQL."""

    host: str
    port: int
    database: str
    username: str
    password: str

    @classmethod
    def from_env(cls) -> PostgresSettings:
        """Load PostgreSQL settings from environment variables."""

        def required(name: str) -> str:
            value = os.getenv(name)

            if not value:
                raise RuntimeError(f"Required environment variable is missing: {name}")

            return value

        port_text = os.getenv(
            "ENTERPRISE_RESEARCH_DB_PORT",
            "5432",
        )

        return cls(
            host=os.getenv(
                "ENTERPRISE_RESEARCH_DB_HOST",
                "127.0.0.1",
            ),
            port=int(port_text),
            database=required("ENTERPRISE_RESEARCH_DB_NAME"),
            username=required("ENTERPRISE_RESEARCH_DB_USER"),
            password=required("ENTERPRISE_RESEARCH_DB_PASSWORD"),
        )

    def sqlalchemy_url(self) -> URL:
        """Build a SQLAlchemy URL without manual password escaping."""
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.username,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database,
        )


def build_async_engine(
    settings: PostgresSettings,
) -> AsyncEngine:
    """Create the PostgreSQL async engine."""
    return create_async_engine(
        settings.sqlalchemy_url(),
        pool_pre_ping=True,
    )


def build_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create an async SQLAlchemy session factory."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )
