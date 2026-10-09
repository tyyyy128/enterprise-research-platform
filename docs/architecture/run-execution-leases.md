# 3E-6D-2 — Fenced Research Run execution leases

## Scope and responsibility

The `research_runs` row is the sole authority for execution ownership and
lifecycle state. The existing read-only recovery inventory remains diagnostic.
LangGraph checkpointing and SSE history are **not** ownership authorities.

This stage adds **exclusive ownership for normal executor dispatch**. It does
**not** auto-replay interrupted stages, guarantee exactly-once external API
calls, or fence writes to other tables such as `research_evidence`. Those
side effects must use idempotency keys / guarded commit paths in 3E-6D-3.

## Schema (migration `b65e6b3d2a90`)

`research_runs` adds:

| Column | Meaning |
| --- | --- |
| `lease_owner_id UUID NULL` | Current worker's opaque ID |
| `lease_expires_at TIMESTAMPTZ NULL` | PostgreSQL wall-clock expiry |
| `lease_fencing_token BIGINT NOT NULL DEFAULT 0` | Monotonically increasing ownership generation |

The nullable owner and expiry must appear together. Existing runs begin at
generation `0`, unowned. Generation **never resets** on release, failure or
completion. Queries are keyed by the Run primary key; there is no poll loop
scanning lease expiration in this phase.

## State transitions and contention

1. A newly created Run is `pending`, unclaimed (`generation=0`).
2. The executor issues a single conditional `UPDATE ... RETURNING` to claim.
   It succeeds only if the Run is nonterminal and its lease is absent or
   expired. Normal dispatch additionally **requires `pending`**.
3. Concurrent claims serialize on the row. PostgreSQL re-evaluates the
   condition after a row-lock wait under the default `READ COMMITTED` isolation.
   Only one claimant wins; the winner's generation increments atomically.
4. A heartbeat conditionally extends the same owner+generation while the
   stored expiry is still in the future. The executor heartbeats during long
   stages. Missing renewals cancel its in-process execution coroutine.
5. Stage/terminal status updates are conditional on **run ID, task ID,
   preceding status, owner ID, generation and unexpired lease**. A stale
   writer raises `ResearchRunLeaseLostError` without modifying Run state.
6. Release is conditional on the same generation and only clears owner+expiry.
   A released/expired earlier generation can never release a replacement.
7. Bare `save()` refuses updates **once a Run has ever been claimed**, even
   after release. It remains for bootstrap and pre-lease test fixtures; it
   must not be used by the executor. This transitional compatibility is
   deliberately narrower than a fully lease-only repository contract.

`clock_timestamp()` is used for conditional expiry checks, including in
`UPDATE ... RETURNING`, instead of a Python worker clock or the fixed start
of a long-running PostgreSQL transaction.

## Recovery safety boundary

`claim_lease(..., allow_inflight=False)` is the normal entry point. An
in-progress `planning/searching/extracting/verifying/writing` Run cannot be
claimed by the normal executor even after its lease expires. The repository
has an **explicit** `allow_inflight=True` override exclusively for a future
recovery supervisor that has **already reconciled** the prior stage's
side effects. **Do not** set it during startup or regular scheduling.

If a stale worker is resumed after a second claimant took ownership, its
heartbeat or guarded update fails. The stale worker will not mark the Run
failed or completed. Cancellation is best-effort: an external LLM request or
side effect may already be committed. Guard those effects separately before
attempting automatic recovery.

When an executor is shut down during an in-progress stage, it releases its
lease, emits an in-process interruption event and keeps the Run's stage state
for later reconciliation. SSE history remains process-local.

## Applying migration and validating

Use a **local development database** whose current Alembic revision is
`224c6bf5fd29` and back it up before applying migrations. With the existing
`ENTERPRISE_RESEARCH_DB_*` environment variables configured and PostgreSQL
running:

```bash
uv run --no-project alembic current
uv run --no-project alembic upgrade head
uv run --no-project alembic current
```

Expected new revision: `b65e6b3d2a90`. Never point these commands at a
production database without a reviewed rollout. A running old service will
not use leases even after the schema upgrade; roll the new executor code out
consistently across all workers before relying on ownership guarantees.

Non-DB unit tests run in the default suite. PostgreSQL concurrency / fencing
integration tests are opt-in:

```bash
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 uv run --no-project pytest \
  tests/infrastructure/test_postgres_run_leases.py \
  tests/infrastructure/test_postgres_run_repository.py -q
```

## Later work

- 3E-6D-3: reconcile interrupted stages, stage-level idempotency and a
  cross-table fencing strategy for artifact writes.
- 3E-6D-4: checkpoint / Run-status authority and handoff boundaries.
- 3E-6D-5: kill/restart and concurrent-worker fault injection with live PG.
