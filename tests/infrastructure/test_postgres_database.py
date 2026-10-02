from __future__ import annotations

import pytest

from enterprise_research.infrastructure.postgres import (
    PostgresSettings,
)


def test_postgres_settings_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_DB_HOST",
        "db.internal",
    )
    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_DB_PORT",
        "5544",
    )
    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_DB_NAME",
        "research",
    )
    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_DB_USER",
        "research_user",
    )
    monkeypatch.setenv(
        "ENTERPRISE_RESEARCH_DB_PASSWORD",
        "secret",
    )

    settings = PostgresSettings.from_env()

    assert settings.host == "db.internal"
    assert settings.port == 5544
    assert settings.database == "research"
    assert settings.username == "research_user"
    assert settings.password == "secret"


def test_sqlalchemy_url_uses_asyncpg() -> None:
    settings = PostgresSettings(
        host="127.0.0.1",
        port=5432,
        database="enterprise_research",
        username="enterprise_research",
        password="p@ss:word",
    )

    url = settings.sqlalchemy_url()

    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "127.0.0.1"
    assert url.port == 5432
    assert url.database == "enterprise_research"
    assert url.username == "enterprise_research"
    assert url.password == "p@ss:word"
