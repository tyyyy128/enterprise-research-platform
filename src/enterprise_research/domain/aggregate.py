"""Aggregate root for one enterprise research task."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from pydantic import BaseModel, Field

from enterprise_research.domain.errors import (
    DuplicateEntityError,
    InvalidReportError,
    UnknownClaimError,
    UnknownEvidenceError,
    UnknownSourceError,
)
from enterprise_research.domain.models import (
    Claim,
    ClaimStatus,
    Evidence,
    ResearchReport,
    ResearchTask,
    ResearchTaskStatus,
    Source,
    utc_now,
)


class ResearchAggregate(BaseModel):
    """Owns the complete evidence graph for one research task."""

    task: ResearchTask
    sources: dict[UUID, Source] = Field(default_factory=dict)
    evidence: dict[UUID, Evidence] = Field(default_factory=dict)
    claims: dict[UUID, Claim] = Field(default_factory=dict)
    report: ResearchReport | None = None

    @classmethod
    def create(cls, query: str) -> ResearchAggregate:
        """Create a new research aggregate."""
        return cls(task=ResearchTask(query=query))

    def add_source(self, source: Source) -> Source:
        """Attach a source to this research task."""
        if source.source_id in self.sources:
            raise DuplicateEntityError(f"Source already exists: {source.source_id}")

        self.sources[source.source_id] = source
        self.task.source_ids.append(source.source_id)
        self._touch()

        return source

    def add_evidence(self, evidence: Evidence) -> Evidence:
        """Attach evidence whose source must already belong to the task."""
        if evidence.evidence_id in self.evidence:
            raise DuplicateEntityError(f"Evidence already exists: {evidence.evidence_id}")

        if evidence.source_id not in self.sources:
            raise UnknownSourceError(f"Evidence references unknown source: {evidence.source_id}")

        self.evidence[evidence.evidence_id] = evidence
        self.task.evidence_ids.append(evidence.evidence_id)
        self._touch()

        return evidence

    def add_claim(self, claim: Claim) -> Claim:
        """Attach a claim after validating all referenced evidence."""
        if claim.claim_id in self.claims:
            raise DuplicateEntityError(f"Claim already exists: {claim.claim_id}")

        self._ensure_evidence_exists(claim.evidence_ids)

        self.claims[claim.claim_id] = claim
        self.task.claim_ids.append(claim.claim_id)
        self._touch()

        return claim

    def support_claim(
        self,
        claim_id: UUID,
        evidence_ids: Sequence[UUID],
        *,
        confidence: float,
    ) -> Claim:
        """Mark a claim as supported by known evidence."""
        claim = self._require_claim(claim_id)

        unique_evidence_ids = list(dict.fromkeys(evidence_ids))
        self._ensure_evidence_exists(unique_evidence_ids)

        updated_claim = Claim.model_validate(
            {
                **claim.model_dump(),
                "status": ClaimStatus.SUPPORTED,
                "evidence_ids": unique_evidence_ids,
                "confidence": confidence,
            }
        )

        self.claims[claim_id] = updated_claim
        self._touch()

        return updated_claim

    def trace_claim(
        self,
        claim_id: UUID,
    ) -> list[tuple[Evidence, Source]]:
        """Resolve a claim to its evidence and original sources."""
        claim = self._require_claim(claim_id)

        lineage: list[tuple[Evidence, Source]] = []

        for evidence_id in claim.evidence_ids:
            evidence = self._require_evidence(evidence_id)
            source = self._require_source(evidence.source_id)
            lineage.append((evidence, source))

        return lineage

    def build_report(
        self,
        *,
        title: str,
        summary: str = "",
        claim_ids: Sequence[UUID] | None = None,
    ) -> ResearchReport:
        """Build a report from supported claims only."""
        selected_claim_ids = (
            list(claim_ids)
            if claim_ids is not None
            else [claim_id for claim_id, claim in self.claims.items() if claim.status == ClaimStatus.SUPPORTED]
        )

        if not selected_claim_ids:
            raise InvalidReportError("A research report requires at least one supported claim.")

        selected_claims = [self._require_claim(claim_id) for claim_id in selected_claim_ids]

        unsupported = [claim.claim_id for claim in selected_claims if claim.status != ClaimStatus.SUPPORTED]

        if unsupported:
            raise InvalidReportError("Research reports may only contain supported claims.")

        report = ResearchReport(
            task_id=self.task.task_id,
            title=title,
            summary=summary,
            claim_ids=selected_claim_ids,
        )

        self.report = report
        self.task.report_id = report.report_id
        self.task.status = ResearchTaskStatus.COMPLETED
        self._touch()

        return report

    def _ensure_evidence_exists(
        self,
        evidence_ids: Sequence[UUID],
    ) -> None:
        missing = [evidence_id for evidence_id in evidence_ids if evidence_id not in self.evidence]

        if missing:
            missing_text = ", ".join(str(item) for item in missing)
            raise UnknownEvidenceError(f"Claim references unknown evidence: {missing_text}")

    def _require_source(self, source_id: UUID) -> Source:
        try:
            return self.sources[source_id]
        except KeyError as exc:
            raise UnknownSourceError(f"Unknown source: {source_id}") from exc

    def _require_evidence(self, evidence_id: UUID) -> Evidence:
        try:
            return self.evidence[evidence_id]
        except KeyError as exc:
            raise UnknownEvidenceError(f"Unknown evidence: {evidence_id}") from exc

    def _require_claim(self, claim_id: UUID) -> Claim:
        try:
            return self.claims[claim_id]
        except KeyError as exc:
            raise UnknownClaimError(f"Unknown claim: {claim_id}") from exc

    def _touch(self) -> None:
        self.task.updated_at = utc_now()
