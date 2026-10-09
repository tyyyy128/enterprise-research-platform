# 3E-6D-1 — Persisted Research Run recovery contract

## Source of truth

- **PostgreSQL `research_runs`** stores `run_id`, `task_id`, status,
  `created_at`, `updated_at`, `started_at`, `completed_at`, and error.
- **In-memory repository** implements the same interface for development and tests;
  its contents **do not** survive process restarts.
- **SSE events and executor tasks are process-local**, not durable evidence of
  whether a stage finished. After a restart, query the persisted Run state.
- LangGraph checkpoints, once introduced, will store agent execution state;
  they must not independently compete with the Run lifecycle for ownership.

## What the status means

The current executor writes a new stage status **before** calling the stage
runner. Therefore `planning`, `searching`, `extracting`, `verifying`, and
`writing` mean **entered that stage**, *not* "its effects were committed".

| Persisted status | Inventory disposition | Safe automatic action |
| --- | --- | --- |
| `pending` | `ownership_claim_required` | None without an exclusive claim |
| Any in-progress stage | `stage_reconciliation_required` | None until the stage side effects are reconciled |
| `completed`, `failed` | Omitted | Never replay |

In particular:

1. An old `updated_at` is **not** a dead-worker signal: it records the last
   transition, not a heartbeat.
2. A `pending` run may be scheduled in another live process. It must be claimed
   atomically **before** execution is allowed.
3. A stage can have made externally visible changes before a crash. The
   persisted status alone cannot tell whether retrying it is safe.
4. The inventory is **read-only**: it never calls `executor.start()`, advances
   status, emits a terminal event, or marks a run as failed.
5. A restart does not recreate a previous process's SSE event history. Reconnect
   to the run state endpoint to refresh status; historical progress is not
   reconstructible from this inventory.

## Interface and operational boundary

`ResearchRunApplicationService.inspect_unfinished_runs(limit=100)` returns
immutable `ResearchRunRecoveryCandidate` values ordered by `(updated_at,
run_id)`, oldest first. An optional `task_id` restricts the scan to one task. The maximum inspection batch is 500; a full cursor-
paginated inventory is deliberately out of scope. Database adapters return
isolated domain snapshots and exclude terminal runs.

At extension startup **only in PostgreSQL mode**, the service inspects the first
batch and warns if unfinished runs exist. It does **not** infer that they are
orphaned or try to resume them. No unauthenticated administrative API is added.

## Work reserved for later stages

- **3E-6D-2**: durable exclusive claim, lease heartbeat, fencing tokens and
  guarded Run transitions are now implemented; see
  [`run-execution-leases.md`](run-execution-leases.md).
- **3E-6D-3**: deterministic reconciliation rules and idempotent replay from
  stage-specific safe boundaries; cancellation vs. crash semantics.
- **3E-6D-4**: define boundaries between Run status and LangGraph checkpoint
  persistence, keeping a single authority for execution ownership.
- **3E-6D-5**: kill/restart and multi-worker fault injection with a real DB.

## Local verification

Run the recovery unit tests without PostgreSQL, then opt into DB integration
checks using `ENTERPRISE_RESEARCH_RUN_DB_TESTS=1` and your existing
`ENTERPRISE_RESEARCH_DB_*` connection settings. No migration is required by
this read-only inventory change.
