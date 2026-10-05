"""Domain errors for enterprise research."""


class ResearchDomainError(ValueError):
    """Base exception for research domain rule violations."""


class DuplicateEntityError(ResearchDomainError):
    """Raised when an entity ID already exists in the aggregate."""


class UnknownSourceError(ResearchDomainError):
    """Raised when evidence references an unknown source."""


class UnknownEvidenceError(ResearchDomainError):
    """Raised when a claim references unknown evidence."""


class UnknownClaimError(ResearchDomainError):
    """Raised when a claim cannot be found."""


class InvalidReportError(ResearchDomainError):
    """Raised when a report violates research domain rules."""


class InvalidResearchRunTransitionError(ResearchDomainError):
    """Raised when a research run attempts an invalid state transition."""
