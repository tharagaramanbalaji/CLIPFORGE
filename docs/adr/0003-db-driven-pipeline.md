# ADR-0003: Pipeline state lives in Postgres; Celery only executes stage tasks

**Status:** Accepted, 2026-11-19

## Context
Processing a video is a DAG of stages (probe, proxy, transcribe, diarize, ...). We need resumability, per-stage retries, idempotency and visible progress.

## Decision
`pipeline_runs` and `stage_runs` tables hold all state. An idempotent `orchestrator.advance(run_id)`, guarded by a Postgres advisory lock, queues the stages whose dependencies are done. Each Celery task runs one stage and then calls `advance`. We don't use Celery canvas (chains/chords) or the Celery result backend.

Each stage has an idempotency key: a hash of the stage, the inputs, the config and the engine version. If an artifact for that key already exists, the stage is skipped.

## Consequences
- A crash mid-pipeline resumes from the failed stage.
- Celery could be swapped (e.g. for Dramatiq) without touching the engine or orchestrator.
