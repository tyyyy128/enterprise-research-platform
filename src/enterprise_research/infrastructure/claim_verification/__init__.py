"""3G evidence-quoted, OpenAI-compatible Claim verification adapter."""

from enterprise_research.infrastructure.claim_verification.openai_compatible import (
    ClaimVerifierAdapterError,
    OpenAICompatibleClaimVerifier,
    OpenAICompatibleClaimVerifierSettings,
)

__all__ = [
    "ClaimVerifierAdapterError",
    "OpenAICompatibleClaimVerifier",
    "OpenAICompatibleClaimVerifierSettings",
]
