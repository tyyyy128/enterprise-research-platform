"""Application-layer errors."""


class ResearchApplicationError(RuntimeError):
    """Base exception for research application errors."""


class ResearchTaskNotFoundError(ResearchApplicationError):
    """Raised when a requested research task does not exist."""


class ResearchRunNotFoundError(ResearchApplicationError):
    """Raised when a requested research run does not exist."""


class ResearchPlanningError(ResearchApplicationError):
    """Raised when research planning cannot be completed safely."""


class ResearchSearchError(ResearchApplicationError):
    """Raised when automated research search cannot complete safely."""
