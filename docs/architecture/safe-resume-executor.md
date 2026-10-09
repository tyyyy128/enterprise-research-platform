# 3E-6D-3C — Conservative Safe Resume Executor

## Authority and scope

- `research_runs` (PostgreSQL) remains the authority for Run status, Lease owner,
  expiry and fencing generation. Stage Journal records attempted work.
- Plan, task Sources and fetched Documents are inspected as persisted artifacts;
  the inspector is **read-only**. SSE events are neither durable checkpoints nor
  authority to resume a Run.
- This is an **explicit, trusted internal** `await executor.resume(task_id, run_id)`
  workflow, not automatic startup replay and not a public HTTP endpoint. The
  process must be configured with its current repositories, stage runner and
  a `ResearchSafeResumeInspector` (the plugin wires this by default).
- A successful `resume()` call means a background job was scheduled, **not**
  that the full Run has completed.

## Decision protocol

1. Inspect the scoped persisted Run, Stage Journal and relevant artifacts **without
   claiming anything**. Terminal Runs are rejected; pending Runs go through the
   existing `start()` path instead.
2. For every stage through the current one, require exactly **one** journal
   attempt with `completed` outcome. Any missing, failed, duplicate or in-doubt
   (`started`) attempt is a **pause**, even if an artifact exists. Later-stage
   journal entries are also a pause.
3. A matching persisted Plan (`task_id`, `run_id`, questions) is mandatory.
   Check additional artifacts according to the current stage:

   | Current persisted stage | Proof needed | Next stage |
   | --- | --- | --- |
   | Planning | Completed Planning journal + persisted run-scoped Plan | Searching |
   | Searching | Above + Source metadata contains a search hit for **every** planned question from this Run | Extracting |
   | Extracting | Above + a matching persisted retrieval outcome for **every** web source in the Task, with at least one success; failed outcomes must belong to this Run | Verifying |
   | Verifying | **Pause** — zero Evidence can be legitimate; there is no durable per-run evidence-completion manifest | — |
   | Writing | **Pause** — no durable finalized report artifact is presently produced | — |

4. A READY decision is **not** ownership. Explicit resume atomically requests
   an in-flight Lease (`allow_inflight=True`), requiring no current unexpired
   owner. The claim increments the fencing token.
5. After claiming, read the entire proof **again**. A changed Run, journal,
   stage, or artifact fingerprint causes release and **no dispatch**. This closes
   the ordinary time-of-check/time-of-use window; it does not make all legacy
   administrative `save()` calls or other Run-owned Task updates serializable.
6. Schedule work beginning with the **next** stage only, under the new Lease.
   Existing Executor heartbeat, Stage Journal, fenced business writes, and
   failure/interruption handling remain in force. The just-reconciled stage
   is never invoked again by this entry point.
7. On SSE, a recovered job publishes `run.resumed`, not a fictitious
   `run.created`. Within a surviving process, a previously interrupted stream
   can reopen with monotonically increasing event IDs. Across restarts event
   history is **not retained**; clients should fetch the Run snapshot.

## Failure windows and intentionally unsupported behavior

- Crash after business write but **before** Stage Journal `completed`: **PAUSE**.
  We cannot assume the stage ran exactly once.
- Crash after `completed` journal but artifact missing or inconsistent:
  **PAUSE**. Journal commit is not an atomic multi-table/business-outcome
  certificate.
- Expired Lease while stage result is uncertain: **PAUSE**. TTL does not
  authorize replay. A live Lease blocks explicit takeover.
- Crash just after claiming a new Lease but before executing the next stage:
  on a subsequent explicit resume, the same proven previous stage can be
  inspected again after the claim expires; it is not replayed.
- A crash after *entering* the next stage but before `start_stage()` logs its
  attempt is conservatively paused (not automatically retried).
- No attempt is made to resume Verifying or Writing, because their durable
  completion manifests are not yet sufficient for reliable reconciliation.
- Source/Document data are partly shared across Runs of one Task. This feature
  does **not** provide task-level serialization, a globally atomic snapshot of
  several repositories, external API exactly-once execution, or rollback of
  search/LLM calls. These require later work and may warrant stricter
  run-scoped manifests.
- This phase adds **no database migration**; it uses the existing
  `7c3a1e9d4b62` Stage Journal/Lease schema.

## Operations and validation

Only invoke explicit `executor.resume(task_id, run_id)` from a trusted internal
operation **after** reviewing the inspector decision. Do not call underlying
`claim_lease(allow_inflight=True)` directly from HTTP or startup code.

Default checks:

```bash
uv run --no-project pytest tests/application/test_safe_resume.py \
  tests/application/test_safe_resume_events.py -q
uv run --no-project pytest -q
```

With the *migrated local development* PostgreSQL database, credentials loaded,
and any required backup completed:

```bash
uv run --no-project alembic current
ENTERPRISE_RESEARCH_RUN_DB_TESTS=1 uv run --no-project pytest \
  tests/infrastructure/test_postgres_safe_resume.py \
  tests/infrastructure/test_postgres_stage_journal.py \
  tests/infrastructure/test_postgres_run_leases.py -q
```

The Postgres test intentionally uses independently constructed repositories and
competing Executors. The default test suite skips real DB tests unless opted in.
