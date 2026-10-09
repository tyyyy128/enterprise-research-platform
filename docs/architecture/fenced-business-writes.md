# 3E-6D-3B — Fenced business writes

## Goal

An old executor generation must not commit its **research-stage business data**
when a new owner has claimed its Run.  Run status fencing and the stage journal
(from 3A) alone do not protect Plan, Source, Document or Evidence persistence.

## Ownership path

1. The executor claims a `ResearchRunLease` and enters a stage.
2. It records a STARTED stage attempt before executing work.
3. It passes the same immutable `(run_id, owner_id, fencing_token)` capability to
   `ResearchPipelineStageRunner.run_stage(..., lease=lease)`.
4. The application service performs external work and calls the **guarded**
   repository save method for stage-generated artifacts.
5. In PostgreSQL, the guarded method runs `SELECT ... FOR UPDATE OF research_runs`
   on the current Run row with matching task, stage, owner, fencing token and
   database-time lease expiration **inside the transaction that writes the
   artifact**.  If the match fails, the complete transaction rolls back.
6. In-memory repositories use the same Run repository lock for lease claims,
   guarded transitions and stage artifact persistence. These semantics are
   for deterministic unit tests; they are not a distributed lock.

The SQL lock serializes Run generation changes with effect commits.  It does
**not** extend an expiring lease: long external work must continue to use the
executor's heartbeat.  The lock is released on transaction commit/rollback.

| Stage | Guarded persistence | Correlation |
|---|---|---|
| Planning | `ResearchPlanRepository.save_guarded` | `plan.run_id` and `plan.task_id` |
| Searching | `ResearchRepository.save_guarded` | aggregate task and Run lease |
| Extracting | `RetrievedDocumentRepository.save_guarded` | source belongs to lease's task |
| Verifying | `ResearchRepository.save_guarded` | aggregate task and Run lease |
| Writing | Not implemented in the current pipeline | Must use same guard in the future |

`save()` remains as a **privileged, legacy/bootstrap/administrative method**
for task setup and existing tests. The background executor always passes its
lease through the Pipeline, and its application services select `save_guarded`
when the lease is supplied. Custom runners which write business data must adopt
this protocol; **direct `save()` or raw SQL from a Worker is not fenced**. Do not
use the ordinary method from an untrusted or resumed stage worker. Enforcing
least-privilege database roles or eliminating unguarded writes entirely is
separate hardening work. This is a correctness protocol for the built-in
executor, not an authorization boundary against arbitrary code holding
application database credentials.

## Failure / race semantics

- **Lease expired, wrong owner, stale token, or wrong stage**: guarded commit
  raises `ResearchRunLeaseLostError` before any stage artifact is written.
- **Wrong document source ownership**: PostgreSQL rejects a document whose
  `ResearchSourceRow.task_id` does not match the leased research task.
- **Concurrent takeover**: the claim and the guarded save serialize on the
  Run row, so a stale generation cannot commit after takeover.
- **Normal provider call succeeds but guarded write fails**: stage journal does
  not imply a durable artifact; reconciliation is still required.
- **Same task, two distinct active Runs**: Run-scoped fencing does not yet
  prevent aggregate-level lost updates across those two Runs. Until task-level
  serialization/versioning exists, do not run multiple research executions
  that mutate the same Task concurrently.

## Explicit non-goals

No automatic stage replay; no exactly-once external LLM/search/fetch calls; no
cross-Run optimistic aggregate revisions; no cross-process SSE replay. Follow-up
3E-6D-3C reconciles persisted artifacts with journal attempts, defines safe
resume rules and external idempotency keys.

## Validation

- Default unit tests exercise the actual Pipeline/Executor path and memory
  stale-token, wrong-task, wrong-stage and row-lock-like serialization.
- Opt-in PostgreSQL tests require a migrated local database and cover Plan,
  Document and Aggregate save/reject behavior with independent session usage.
- PostgreSQL query compilation checks `FOR UPDATE`, ownership, fencing and
  database-time predicates, but cannot replace the live database tests.
