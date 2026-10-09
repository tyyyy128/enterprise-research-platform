# 3E-6D-3A — Stage Execution Journal

## Why a journal is required

`research_runs.status` records **entry into** a pipeline stage, not successful
completion. A process can terminate after the status transition, during an LLM
call, or after the stage runner returns but before its result is durably marked.
Each case looks the same without additional evidence.

The stage journal records one attempt per `(run_id, stage, fencing_token)` in
`research_run_stage_attempts`. Fencing tokens are monotonic generations obtained
from the PostgreSQL Run Lease implementation in 3E-6D-2.

## Protocol

1. Claim a `pending` Run and start the Lease heartbeat.
2. Advance its persisted Run status under the current Lease.
3. **Start** an attempt under the same Lease, stage and fencing generation. If
   the durable insert fails, do not call the stage runner.
4. Invoke the stage runner.
5. On normal return, atomically update the attempt to `completed` **under a
   current, unexpired Lease**; on a stage exception, update it to `failed` with
   an error (then separately mark the Run failed). A duplicate finish fails.
6. Only after successful journal completion does the Executor move into the
   next stage. `completed` Run is a terminal status, **not** a journal stage.

All journal mutations start a short transaction, obtain a `FOR UPDATE` lock on
`research_runs`, check the stage plus owner and fencing token, and compare the
expiration with PostgreSQL `clock_timestamp()`. The lease check and the journal
mutation commit in the **same transaction**. Renew, release, acquire and
Run-status transitions take conflicting locks on the same Run row.

The in-memory repository implements the same contract under one async lock,
with an injectable clock for deterministic tests. It is not durable across
processes.

## Interpreting persisted attempts

| Attempt result | Meaning | Recovery action in 3E-6D-3C |
| --- | --- | --- |
| No entry | The start record did not commit | Inspect the Run status and stage artifacts; do not infer safe replay |
| `started` | Stage entered; may have performed external work | Reconcile side effects before any takeover/replay |
| `failed` | Runner raised and the failure record committed | Apply retry policy after reconciliation |
| `completed` | Runner returned and journal finish committed | Verify durable business artifacts before skipping |

An attempt can be left `started` after cancellation, process death, Lease loss,
or even after successful external work whose journal commit failed. An expired
or superseded generation cannot later change its attempt, including to
`failed`. An explicit new generation may create a **new** attempt without
modifying the old attempt. There is no automatic in-flight replay in this phase.

## Deliberate consistency boundary

**`completed` is evidence of a successful *journal* commit, NOT an atomic
commit of Plan, Source, Document, Evidence or external LLM/search effects.**
Those writes currently use other repository transactions. Stage runner return
can also conceal earlier partial writes. 3E-6D-3B will extend fencing to the
business persistence boundary and design transactional artifact markers and
external-call idempotency. 3E-6D-3C will consume this journal for an explicit
reconciliation/resume protocol. This phase makes **no exactly-once claim**.

## Migration and verification

Migration `7c3a1e9d4b62` (parent `b65e6b3d2a90`) adds a new table and
constraints. It does not rebuild the existing Run or business tables. On a
backed-up, migrated **development** PostgreSQL database, set
`ENTERPRISE_RESEARCH_RUN_DB_TESTS=1` and run
`tests/infrastructure/test_postgres_stage_journal.py` to verify durable reads,
concurrent finishes, and fencing under real row locks. Default tests run without
a database and leave these integration cases skipped.
