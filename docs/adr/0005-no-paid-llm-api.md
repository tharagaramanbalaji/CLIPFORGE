# ADR-0005: Highlight analysis uses no pay-per-token API by default

**Status:** Accepted (direction); backend choice is finalized in Phase 4. Date: 2026-11-19.

## Context
The owner wants no costs beyond their existing Claude plan. The Anthropic Messages API is billed per token, separately from Claude plans, so it can't be the default.

## Decision
The LLM step sits behind a `HighlightAnalyzer` interface, which takes a transcript window and returns validated candidates (ADR-0001). There are three backends:

1. **`claude_code` (primary):** runs Claude Code in headless mode (`claude -p`, JSON output) on the host, signed in with the owner's plan. A small host-side runner picks up queued analysis jobs (files or a DB queue) and writes the results back. It runs on the host because the CLI login lives there, not inside the containers.
2. **`ollama` (free fallback):** a local model on the RTX 3060, e.g. a 14B model at 4-bit quantization. Quality is lower; it's useful for bulk pre-filtering or when plan limits are reached.
3. **`anthropic_api` (optional, disabled):** kept for completeness. It needs an explicit config flag and an API key.

Plan limits are shared with development work, so we conserve them:
- Large windows (about 15 minutes), so a 3-hour video needs about 12 discovery calls.
- Cheap deterministic pre-filtering with audio-energy and laughter signals, which skips dead air.
- Pass-2 refinement only for the top-N candidates (default 10).
- Vision analysis off by default.
- Results are cached by idempotency key, so nothing is analysed twice.

## Consequences
- Highlight runs are slower and are rate-limited by the plan. Clipping is offline batch work, so that's acceptable.
- The structured-output guarantees of the API aren't available, so validation is strict: parse JSON, validate with Pydantic, re-ask once, then mark the run failed.
- The eval harness (Phase 4) compares the `claude_code` and `ollama` backends on the same labeled set.
