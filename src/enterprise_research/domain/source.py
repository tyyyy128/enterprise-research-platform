from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from uuid import UUID, uuid4


class SourceType(str, Enum):
    WEB_PAGE = "web_page"
    PDF_REPORT = "pdf_report"
    INTERNAL_DOCUMENT = "internal_document"


class TrustLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Source:

    url: str
    title: str

    id: UUID = field(
        default_factory=uuid4
    )

    publisher: str | None = None

    source_type: SourceType = (
        SourceType.WEB_PAGE
    )

    trust_level: TrustLevel = (
        TrustLevel.MEDIUM
    )

    content_hash: str | None = None

    collected_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )
