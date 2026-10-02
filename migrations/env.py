"""Alembic migration environment."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import AsyncConnection

from enterprise_research.infrastructure.postgres import (
    Base,
    PostgresSettings,
    build_async_engine,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without creating a DB connection."""
    settings = PostgresSettings.from_env()

    context.configure(
        url=settings.sqlalchemy_url().render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={
            "paramstyle": "named",
        },
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(
    connection: AsyncConnection,
) -> None:
    """Configure Alembic with a synchronous facade."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run online migrations using SQLAlchemy async engine."""
    settings = PostgresSettings.from_env()
    engine = build_async_engine(settings)

    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live PostgreSQL database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
