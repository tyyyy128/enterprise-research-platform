from datetime import datetime, UTC

from sqlalchemy import (
    DateTime,
    String,
    Text,
)
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from enterprise_research.infrastructure.database.connection import (
    Base,
)


class ResearchTaskModel(Base):

    __tablename__ = "research_tasks"


    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )


    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )


    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )


    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )


    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
