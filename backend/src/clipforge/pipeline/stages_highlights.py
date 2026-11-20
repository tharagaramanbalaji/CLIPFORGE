"""Highlights pipeline stages: highlight_windows (pass 1) -> highlight_refine (pass 2) -> rank."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from clipforge.config import get_settings
from clipforge.db.models import (
    Creator,
    HighlightCandidate,
    LlmCall,
    MediaAsset,
    Scene,
    SignalSeries,
    Speaker,
    Transcript,
    TranscriptSegment,
)
from clipforge.engine.highlights.llm import BridgeClient, LlmClient, LlmResponse, OllamaClient
from clipforge.engine.highlights.run import (
    HighlightConfig,
    Inputs,
    discover,
    discover_signals,
    finalize,
    for_content,
    refine,
    shortlist,
)
from clipforge.engine.highlights.schemas import Candidate, Category, Seg
from clipforge.engine.highlights.scoring import ScoringProfile
from clipforge.engine.highlights.windowing import Peak, find_peaks
from clipforge.errors import Canceled, ClipForgeError
from clipforge.pipeline.orchestrator import StageContext
from clipforge.pipeline.stages import storage

LOW_CONFIDENCE = 0.5


# --- inputs --------------------------------------------------------------------------------------------


def current_transcript(session: Session, asset_id: uuid.UUID) -> Transcript | None:
    return session.scalar(
        select(Transcript)
        .where(Transcript.media_asset_id == asset_id, Transcript.is_current)
        .order_by(Transcript.created_at.desc())
        .limit(1)
    )


def _load_series(session: Session, asset_id: uuid.UUID, kind: str) -> tuple[list[float], int] | None:
    import io

    import numpy as np

    row = session.scalar(
        select(SignalSeries).where(SignalSeries.media_asset_id == asset_id, SignalSeries.kind == kind)
    )
    if row is None or not storage().exists(row.values_storage_key):
        return None
    values = np.load(io.BytesIO(storage().local_path(row.values_storage_key).read_bytes()))
    return [float(v) for v in values], row.hop_ms


def build_inputs(session: Session, asset: MediaAsset) -> Inputs:
    transcript = current_transcript(session, asset.id)
    if transcript is None:
        raise ClipForgeError("No transcript: run analysis before highlight detection.")
    rows = session.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.transcript_id == transcript.id)
        .order_by(TranscriptSegment.idx)
    )
    segs = [
        Seg(
            idx=r.idx,
            start_ms=r.start_ms,
            end_ms=r.end_ms,
            text=r.text,
            speaker=r.speaker_label,
            words=tuple((int(w["s"]), int(w["e"])) for w in r.words),
            low_confidence=(r.confidence is not None and r.confidence < LOW_CONFIDENCE)
            or "repetitive" in (r.flags or []),
        )
        for r in rows
    ]
    peaks: list[Peak] = []
    for kind in ("laughter", "excitement"):
        s = _load_series(session, asset.id, kind)
        if s:
            peaks += find_peaks(s[0], s[1], kind, threshold=0.5)
    energy = _load_series(session, asset.id, "energy")
    if energy and energy[0]:
        vals = sorted(energy[0])
        loud_threshold = vals[int(0.995 * (len(vals) - 1))]  # top 0.5 % of 100 ms frames
        peaks += find_peaks(
            energy[0], energy[1], "loud", threshold=max(loud_threshold, -20.0), min_gap_ms=30_000, limit=100
        )
    cuts = list(
        session.scalars(select(Scene.start_ms).where(Scene.media_asset_id == asset.id, Scene.idx > 0))
    )
    names = {
        sp.label: sp.display_name
        for sp in session.scalars(select(Speaker).where(Speaker.media_asset_id == asset.id))
        if sp.display_name
    }
    creator = session.get(Creator, asset.project.creator_id)
    context = (
        f"{creator.name}" + (f" - {creator.notes}" if creator and creator.notes else "") if creator else ""
    )
    return Inputs(
        segs=segs,
        duration_ms=asset.duration_ms or (segs[-1].end_ms if segs else 0),
        peaks=sorted(peaks, key=lambda p: p.ms),
        cuts=cuts,
        speaker_names=names,
        creator_context=context,
    )


def highlight_config(run_config: dict[str, Any]) -> HighlightConfig:
    s = get_settings()
    return for_content(
        str(run_config.get("content_type", s.highlight_content_type)),
        window_ms=int(run_config.get("window_min", s.highlight_window_min)) * 60_000,
        refine_top_n=int(run_config.get("refine_top_n", s.highlight_refine_top_n)),
    )


def run_settings_snapshot(
    transcript_id: uuid.UUID, backend: str | None = None, content_type: str | None = None
) -> dict[str, Any]:
    """Stored as the highlights run's config: pins everything that changes the result (idempotency key)."""
    from clipforge.engine.highlights import prompts

    s = get_settings()
    b = backend or s.llm_backend.value
    models = (
        {"discover_model": s.llm_discover_model, "refine_model": s.llm_refine_model}
        if b == "claude_code"
        else {"discover_model": s.ollama_model, "refine_model": s.ollama_model}
    )
    return {
        "backend": b,
        **models,
        "transcript_id": str(transcript_id),
        "window_min": s.highlight_window_min,
        "refine_top_n": s.highlight_refine_top_n,
        "content_type": content_type or s.highlight_content_type,
        "prompts": [prompts.DISCOVER_VERSION, prompts.REFINE_VERSION, prompts.JUDGE_VERSION],
    }


# --- LLM clients ---------------------------------------------------------------------------------------


def _default_client(backend: str, model: str) -> LlmClient:
    s = get_settings()
    if backend == "claude_code":
        if s.llm_bridge_token is None:
            raise ClipForgeError("CLIPFORGE_LLM_BRIDGE_TOKEN is not set.")
        return BridgeClient(s.llm_bridge_url, s.llm_bridge_token.get_secret_value(), model)
    if backend == "ollama":
        return OllamaClient(s.ollama_url, model)
    raise ClipForgeError(f"LLM backend {backend!r} is not enabled (paid API stays off, ADR-0005).")


# Tests replace this with a fake.
client_factory: Callable[[str, str], LlmClient] = _default_client


def _call_logger(ctx: StageContext, backend: str) -> Callable[[str, str, int | None, LlmResponse, str], None]:
    def log(purpose: str, version: str, window_idx: int | None, r: LlmResponse, request: str) -> None:
        call = LlmCall(
            stage_run_id=ctx.stage_run.id,
            media_asset_id=ctx.asset.id,
            purpose=purpose,
            backend=backend,
            model=r.model,
            prompt_version=version,
            window_idx=window_idx,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_read_tokens=r.cache_read_tokens,
            cache_write_tokens=r.cache_write_tokens,
            cost_usd=r.cost_usd,
            latency_ms=r.latency_ms,
            response_text=r.text[:200_000],
        )
        ctx.session.add(call)
        ctx.session.flush()
        key = f"assets/{ctx.asset.id}/llm/{call.id}.txt"
        from clipforge.pipeline.stages_analysis import _put_bytes

        _put_bytes(ctx, key, request.encode())
        call.request_storage_key = key
        ctx.session.commit()  # persist each call as it happens (useful if a later window fails)

    return log


# --- candidate <-> row -------------------------------------------------------------------------------


def _to_row(c: Candidate, ctx: StageContext) -> HighlightCandidate:
    return HighlightCandidate(
        media_asset_id=ctx.asset.id,
        pipeline_run_id=ctx.run.id,
        window_idx=c.window_idx,
        start_segment_idx=c.start_segment,
        end_segment_idx=c.end_segment,
        setup_segment_idx=c.setup_segment,
        start_ms=c.start_ms,
        end_ms=c.end_ms,
        category=c.category.value,
        hook=c.hook,
        description=c.description,
        why_interesting=c.why_interesting,
        standalone_context=c.standalone_context,
        status=c.status,
        scores=c.scores,
        validation_errors=c.errors,
        extra=c.extra,
        title=c.title,
        hook_text=c.hook_text,
    )


def _from_row(r: HighlightCandidate) -> Candidate:
    c = Candidate(
        window_idx=r.window_idx,
        start_segment=r.start_segment_idx,
        end_segment=r.end_segment_idx,
        category=Category(r.category),
        hook=r.hook,
        description=r.description,
        why_interesting=r.why_interesting,
        standalone_context=r.standalone_context,
        scores=dict(r.scores),
        start_ms=r.start_ms,
        end_ms=r.end_ms,
        setup_segment=r.setup_segment_idx,
        errors=list(r.validation_errors),
        status=r.status,
        title=r.title,
        hook_text=r.hook_text,
        extra=dict(r.extra),
    )
    c.extra["row_id"] = str(r.id)
    return c


def _update_row(r: HighlightCandidate, c: Candidate) -> None:
    r.start_segment_idx, r.end_segment_idx, r.start_ms, r.end_ms = (
        c.start_segment,
        c.end_segment,
        c.start_ms,
        c.end_ms,
    )
    r.status, r.scores, r.standalone_context = c.status, c.scores, c.standalone_context
    r.title, r.hook_text, r.description = c.title, c.hook_text, c.description
    r.final_score, r.rank, r.validation_errors = c.final_score, c.rank, c.errors
    r.extra = {k: v for k, v in c.extra.items() if k != "row_id"}


def _rows(ctx: StageContext, statuses: tuple[str, ...]) -> list[HighlightCandidate]:
    return list(
        ctx.session.scalars(
            select(HighlightCandidate).where(
                HighlightCandidate.pipeline_run_id == ctx.run.id, HighlightCandidate.status.in_(statuses)
            )
        )
    )


def _check_cancel(ctx: StageContext, fraction: float) -> None:
    if not ctx.report_progress(fraction):
        raise Canceled("canceled")


# --- stages --------------------------------------------------------------------------------------------


def highlight_windows_stage(ctx: StageContext) -> dict[str, Any]:
    rc = ctx.run.config
    cfg = highlight_config(rc)
    inp = build_inputs(ctx.session, ctx.asset)
    backend = str(rc.get("backend", get_settings().llm_backend.value))
    client = client_factory(backend, str(rc.get("discover_model", "")))
    ctx.session.execute(delete(HighlightCandidate).where(HighlightCandidate.pipeline_run_id == ctx.run.id))
    _check_cancel(ctx, 0.02)
    log = _call_logger(ctx, backend)
    cands = discover(inp, cfg, client, log) + discover_signals(inp, cfg, client, log)
    survivors = shortlist(cands, cfg)
    for c in cands:
        ctx.session.add(_to_row(c, ctx))
    by_status: dict[str, int] = {}
    for c in cands:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    return {
        "candidates": len(cands),
        "by_status": by_status,
        "shortlisted": len(survivors),
        "peaks": len(inp.peaks),
        "segments": len(inp.segs),
    }


def highlight_refine_stage(ctx: StageContext) -> dict[str, Any]:
    rc = ctx.run.config
    cfg = highlight_config(rc)
    rows = [r for r in _rows(ctx, ("proposed",)) if not (r.extra or {}).get("not_refined")]
    if not rows:
        return {"refined": 0}
    inp = build_inputs(ctx.session, ctx.asset)
    backend = str(rc.get("backend", get_settings().llm_backend.value))
    client = client_factory(backend, str(rc.get("refine_model", "")))
    cands = [_from_row(r) for r in rows]
    refine(inp, cands, cfg, client, _call_logger(ctx, backend))
    for r, c in zip(rows, cands, strict=True):
        _update_row(r, c)
    return {
        "refined": sum(c.status == "refined" for c in cands),
        "discarded": sum(c.status == "discarded" for c in cands),
    }


def rank_stage(ctx: StageContext) -> dict[str, Any]:
    cfg = highlight_config(ctx.run.config)
    inp = build_inputs(ctx.session, ctx.asset)
    rows = _rows(ctx, ("proposed", "refined"))
    cands = [_from_row(r) for r in rows]
    ranked = finalize(inp, cands, cfg, ScoringProfile())
    for r, c in zip(rows, cands, strict=True):
        _update_row(r, c)
    return {
        "ranked": len(ranked),
        "top": [
            {
                "rank": c.rank,
                "score": c.final_score,
                "start_ms": c.start_ms,
                "end_ms": c.end_ms,
                "title": c.title or c.hook,
            }
            for c in ranked[:10]
        ],
    }
