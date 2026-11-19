# ADR-0001: The LLM references transcript segment/word IDs, never raw timestamps

**Status:** Accepted, 2026-11-19

## Context
Language models drift or invent times when they have to write numeric timestamps. Clip boundaries must be exact and checkable.

## Decision
Transcript windows are shown to the model with stable segment IDs (`s0412`). The model returns segment (and in pass 2, word) indices. The backend converts them to milliseconds from the stored word timings, then validates and snaps them to word and silence boundaries.

## Consequences
- Every boundary can be checked against the source transcript and duration.
- Prompts must carry the IDs, which costs a few extra tokens per line.
- Moments with no speech (pure gameplay) need signal-based anchors (energy peaks, scene cuts) with their own IDs.
