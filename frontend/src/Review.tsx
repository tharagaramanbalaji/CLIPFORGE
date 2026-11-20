// Phase 8 review dashboard: pick a clip, trim on the waveform / transcript, edit text and captions,
// re-render (new version, old ones kept), approve or reject with a reason, download everything approved.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { ApiError, apiGet, apiSend } from "./api/client";
import { SubtitleEditor } from "./Clips";

type Version = { id: string; version: number; preset: string; status: string; created_at: string; error: { message: string } | null };
type Clip = {
  id: string; title: string; hook_text: string | null; start_ms: number; end_ms: number; status: string;
  review: string; reject_reason: string | null; candidate_id: string | null; latest_version: Version | null;
};
type Candidate = {
  id: string; rank: number | null; final_score: number | null; start_ms: number; end_ms: number; category: string;
  title: string | null; hook: string; description: string; why_interesting: string; scores: Record<string, number>;
  extra: { source?: string; judge_reason?: string; refine_rationale?: string };
};
type Word = { w: string; s: number; e: number };
type Seg = { idx: number; start_ms: number; end_ms: number; speaker_label: string | null; words: Word[] | null };
type Audit = { action: string; data: Record<string, unknown>; at: string };

const btn = "rounded bg-zinc-100 px-2.5 py-1 text-xs font-medium text-zinc-900 hover:bg-white disabled:opacity-40";
const ghost = "rounded border border-zinc-700 px-2.5 py-1 text-xs hover:border-zinc-400 disabled:opacity-40";
const input = "rounded bg-zinc-900 px-1.5 py-1 text-xs outline-none focus:ring-1 focus:ring-zinc-500";
const fmt = (ms: number) => `${Math.floor(ms / 60000)}:${((ms % 60000) / 1000).toFixed(1).padStart(4, "0")}`;
const errText = (e: unknown) => (e instanceof Error ? e.message : e ? String(e) : "");
const BADGE: Record<string, string> = {
  approved: "bg-emerald-900 text-emerald-200", rejected: "bg-rose-900 text-rose-200", pending: "bg-zinc-800 text-zinc-300",
};

export function ReviewPage({ mediaId, name, onClose }: { mediaId: string; name: string; onClose: () => void }) {
  const [tab, setTab] = useState<"clips" | "candidates">("clips");
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<string | null>(null);
  const qc = useQueryClient();
  const clips = useQuery({ queryKey: ["media", mediaId, "clips"], queryFn: () => apiGet<Clip[]>(`/media/${mediaId}/clips`), refetchInterval: 5000 });
  const cands = useQuery({
    queryKey: ["media", mediaId, "candidates"],
    queryFn: () => apiGet<Candidate[]>(`/media/${mediaId}/candidates`).catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 404) return [] as Candidate[];
      throw e;
    }),
  });
  const makeClip = useMutation({
    mutationFn: (id: string) => apiSend<Clip[]>("POST", `/media/${mediaId}/clips`, { candidate_ids: [id] }),
    onSuccess: (c) => { setSelected(c[0].id); setTab("clips"); return qc.invalidateQueries({ queryKey: ["media", mediaId] }); },
  });
  const list = (clips.data ?? []).filter((c) => filter === "all" || c.review === filter);
  const clipped = new Set((clips.data ?? []).map((c) => c.candidate_id));
  const current = clips.data?.find((c) => c.id === selected) ?? null;
  const approvedCount = (clips.data ?? []).filter((c) => c.review === "approved").length;

  return (
    <div className="mt-4 rounded-lg border border-zinc-800 p-4">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <button className={ghost} onClick={onClose}>← Back</button>
        <h2 className="font-semibold">Review · {name}</h2>
        <a className={`${btn} ml-auto ${approvedCount ? "" : "pointer-events-none opacity-40"}`} href={`/api/v1/media/${mediaId}/approved.zip`} download>
          Download {approvedCount} approved
        </a>
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(260px,340px)_1fr]">
        <div className="space-y-2">
          <div className="flex gap-2 text-xs">
            <button className={tab === "clips" ? btn : ghost} onClick={() => setTab("clips")}>Clips ({clips.data?.length ?? 0})</button>
            <button className={tab === "candidates" ? btn : ghost} onClick={() => setTab("candidates")}>Candidates ({cands.data?.length ?? 0})</button>
            {tab === "clips" && (
              <select className={`${input} ml-auto`} value={filter} onChange={(e) => setFilter(e.target.value)}>
                {["all", "pending", "approved", "rejected"].map((f) => <option key={f}>{f}</option>)}
              </select>
            )}
          </div>
          {tab === "clips" ? (
            <ul className="max-h-[70vh] space-y-1 overflow-auto">
              {list.length === 0 && <li className="text-xs text-zinc-500">No clips here. Make some from Candidates.</li>}
              {list.map((c) => (
                <li key={c.id}>
                  <button onClick={() => setSelected(c.id)}
                    className={`w-full rounded p-2 text-left text-xs ${selected === c.id ? "bg-zinc-800" : "bg-zinc-900 hover:bg-zinc-800"}`}>
                    <div className="flex items-center gap-2">
                      <span className="flex-1 truncate font-medium">{c.title}</span>
                      <span className={`rounded px-1.5 ${BADGE[c.review] ?? ""}`}>{c.review}</span>
                    </div>
                    <div className="mt-0.5 text-zinc-500">
                      {fmt(c.start_ms)}–{fmt(c.end_ms)} · {c.latest_version ? `v${c.latest_version.version} ${c.latest_version.status}` : c.status}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <ul className="max-h-[70vh] space-y-1 overflow-auto">
              {(cands.data ?? []).map((c) => (
                <li key={c.id} className="rounded bg-zinc-900 p-2 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="text-zinc-500">#{c.rank}</span>
                    <span className="flex-1 truncate font-medium">{c.title ?? c.hook}</span>
                    <span className="tabular-nums text-zinc-400">{c.final_score?.toFixed(2)}</span>
                  </div>
                  <div className="mt-0.5 text-zinc-500">{fmt(c.start_ms)}–{fmt(c.end_ms)} · {c.category}{c.extra.source === "signal" ? " · signal" : ""}</div>
                  <p className="mt-1 text-zinc-400">{c.extra.refine_rationale ?? c.extra.judge_reason ?? c.why_interesting}</p>
                  <p className="mt-1 text-zinc-500">{Object.entries(c.scores).map(([k, v]) => `${k} ${v}`).join(" · ")}</p>
                  <button className={`${ghost} mt-1`} disabled={clipped.has(c.id) || makeClip.isPending} onClick={() => makeClip.mutate(c.id)}>
                    {clipped.has(c.id) ? "clipped" : "Make clip"}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <p className="text-xs text-rose-400">{errText(makeClip.error)}</p>
        </div>
        {current ? <ClipEditor key={current.id} mediaId={mediaId} clip={current} /> : (
          <p className="text-sm text-zinc-500">Select a clip to review it.</p>
        )}
      </div>
    </div>
  );
}

function ClipEditor({ mediaId, clip }: { mediaId: string; clip: Clip }) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["media", mediaId] });
  const [start, setStart] = useState(clip.start_ms);
  const [end, setEnd] = useState(clip.end_ms);
  const [title, setTitle] = useState(clip.title);
  const [hook, setHook] = useState(clip.hook_text ?? "");
  const [source, setSource] = useState(!clip.latest_version || clip.latest_version.status !== "ready");
  const [reason, setReason] = useState("");
  const [opts, setOpts] = useState({ preset: "shorts", frame_mode: "auto", subtitle_style: "bold_pop", watermark: "" });
  const [tab, setTab] = useState<"trim" | "captions" | "versions" | "history">("trim");
  const dirty = start !== clip.start_ms || end !== clip.end_ms || title !== clip.title || hook !== (clip.hook_text ?? "");

  const save = useMutation({
    mutationFn: () => apiSend("PATCH", `/clips/${clip.id}`, { title, hook_text: hook, start_ms: start, end_ms: end }),
    onSuccess: refresh,
  });
  const render = useMutation({
    mutationFn: async () => {
      if (dirty) await apiSend("PATCH", `/clips/${clip.id}`, { title, hook_text: hook, start_ms: start, end_ms: end });
      return apiSend("POST", `/clips/${clip.id}/render`, { ...opts, hook_text: !!hook, watermark: opts.watermark || null });
    },
    onSuccess: () => { setSource(false); return refresh(); },
  });
  const decide = useMutation({
    mutationFn: (kind: "approve" | "reject" | "reopen") =>
      apiSend("POST", `/clips/${clip.id}/${kind}`, kind === "reject" ? { reason } : undefined),
    onSuccess: () => { setReason(""); return refresh(); },
  });
  const v = clip.latest_version;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input className={`${input} flex-1 text-sm`} value={title} onChange={(e) => setTitle(e.target.value)} />
        <span className={`rounded px-2 py-0.5 text-xs ${BADGE[clip.review] ?? ""}`}>{clip.review}</span>
        {clip.reject_reason && <span className="text-xs text-rose-300">“{clip.reject_reason}”</span>}
      </div>
      <div className="grid gap-3 md:grid-cols-[minmax(200px,300px)_1fr]">
        <Player mediaId={mediaId} version={v} source={source} start={start} end={end} />
        <div className="space-y-2 text-xs">
          <div className="flex gap-2">
            <button className={source ? ghost : btn} disabled={!v || v.status !== "ready"} onClick={() => setSource(false)}>Rendered v{v?.version ?? "–"}</button>
            <button className={source ? btn : ghost} onClick={() => setSource(true)}>Source (trim preview)</button>
          </div>
          <label className="block text-zinc-400">Hook text (top, first 3 s)
            <input className={`${input} mt-1 w-full`} value={hook} onChange={(e) => setHook(e.target.value)} />
          </label>
          <div className="flex flex-wrap gap-2">
            <select className={input} value={opts.preset} onChange={(e) => setOpts({ ...opts, preset: e.target.value })}>
              {["shorts", "reels", "tiktok", "square", "wide"].map((p) => <option key={p}>{p}</option>)}
            </select>
            <select className={input} value={opts.frame_mode} onChange={(e) => setOpts({ ...opts, frame_mode: e.target.value })}>
              <option value="auto">auto framing</option><option value="blur_pad">fit + blur</option>
              <option value="crop">center crop</option><option value="two_stack">two people</option>
            </select>
            <select className={input} value={opts.subtitle_style} onChange={(e) => setOpts({ ...opts, subtitle_style: e.target.value })}>
              {["bold_pop", "clean_box", "karaoke", "none"].map((s) => <option key={s}>{s}</option>)}
            </select>
            <input className={`${input} w-28`} placeholder="@watermark" value={opts.watermark} onChange={(e) => setOpts({ ...opts, watermark: e.target.value })} />
          </div>
          <div className="flex flex-wrap gap-2">
            <button className={ghost} disabled={!dirty || save.isPending} onClick={() => save.mutate()}>Save edits</button>
            <button className={btn} disabled={clip.status === "rendering" || render.isPending} onClick={() => render.mutate()}>
              {clip.status === "rendering" ? "Rendering…" : v ? "Re-render (new version)" : "Render"}
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-2 border-t border-zinc-800 pt-2">
            <button className="rounded bg-emerald-600 px-2.5 py-1 font-medium text-white disabled:opacity-40"
              disabled={!v || v.status !== "ready" || clip.review === "approved"} onClick={() => decide.mutate("approve")}>Approve</button>
            <input className={`${input} w-40`} placeholder="reason to reject" value={reason} onChange={(e) => setReason(e.target.value)} />
            <button className="rounded bg-rose-700 px-2.5 py-1 font-medium text-white disabled:opacity-40"
              disabled={!reason.trim()} onClick={() => decide.mutate("reject")}>Reject</button>
            {clip.review !== "pending" && <button className={ghost} onClick={() => decide.mutate("reopen")}>Reopen</button>}
          </div>
          <p className="text-rose-400">{errText(save.error ?? render.error ?? decide.error)}{v?.error ? v.error.message : ""}</p>
        </div>
      </div>
      <div className="flex gap-2 text-xs">
        {(["trim", "captions", "versions", "history"] as const).map((t) => (
          <button key={t} className={tab === t ? btn : ghost} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      {tab === "trim" && <TrimEditor mediaId={mediaId} start={start} end={end} onChange={(s, e) => { setStart(s); setEnd(e); }} />}
      {tab === "captions" && <SubtitleEditor clipId={clip.id} />}
      {tab === "versions" && <Versions clipId={clip.id} />}
      {tab === "history" && <History clipId={clip.id} />}
    </div>
  );
}

function Player({ mediaId, version, source, start, end }: { mediaId: string; version: Version | null; source: boolean; start: number; end: number }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || !source) return;
    const seek = () => { el.currentTime = start / 1000; };
    const stop = () => { if (el.currentTime >= end / 1000) { el.pause(); el.currentTime = start / 1000; } };
    if (el.readyState >= 1) seek(); else el.addEventListener("loadedmetadata", seek, { once: true });
    el.addEventListener("timeupdate", stop);
    return () => el.removeEventListener("timeupdate", stop);
  }, [source, start, end]);
  if (!source && version?.status === "ready") {
    return <video key={version.id} className="max-h-[60vh] w-full rounded bg-black" src={`/api/v1/clip-versions/${version.id}/video`} poster={`/api/v1/clip-versions/${version.id}/thumbnail`} controls />;
  }
  return <video ref={ref} className="w-full rounded bg-black" src={`/api/v1/media/${mediaId}/preview`} controls />;
}

// Waveform of [start-10s, end+10s] with draggable in/out handles; transcript words to snap to.
function TrimEditor({ mediaId, start, end, onChange }: { mediaId: string; start: number; end: number; onChange: (s: number, e: number) => void }) {
  const [view] = useState(() => ({ from: Math.max(0, start - 10_000), to: end + 10_000 }));
  const wf = useQuery({
    queryKey: ["wf", mediaId, view.from, view.to],
    queryFn: () => apiGet<{ peaks: number[] }>(`/media/${mediaId}/waveform?from_ms=${view.from}&to_ms=${view.to}&points=600`),
  });
  const tr = useQuery({
    queryKey: ["tr", mediaId, view.from, view.to],
    queryFn: () => apiGet<{ segments: Seg[] }>(`/media/${mediaId}/transcript?from_ms=${view.from}&to_ms=${view.to}`),
  });
  const words = useMemo(() => (tr.data?.segments ?? []).flatMap((s) => s.words ?? []), [tr.data]);
  const canvas = useRef<HTMLCanvasElement>(null);
  const drag = useRef<"start" | "end" | null>(null);
  const span = view.to - view.from;
  const x = (ms: number, w: number) => ((ms - view.from) / span) * w;

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const g = c.getContext("2d")!;
    const w = (c.width = c.clientWidth * devicePixelRatio), h = (c.height = 90 * devicePixelRatio);
    g.clearRect(0, 0, w, h);
    g.fillStyle = "rgba(16,185,129,0.15)";
    g.fillRect(x(start, w), 0, x(end, w) - x(start, w), h);
    const peaks = wf.data?.peaks ?? [];
    g.fillStyle = "#a1a1aa";
    peaks.forEach((p, i) => { const ph = Math.max(1, p * h * 0.95); g.fillRect((i / peaks.length) * w, (h - ph) / 2, Math.max(1, w / peaks.length - 0.5), ph); });
    g.fillStyle = "#e4e4e7";
    for (const wd of words) g.fillRect(x(wd.s, w), h - 6, Math.max(1, x(wd.e, w) - x(wd.s, w)), 4);
    g.fillStyle = "#10b981";
    g.fillRect(x(start, w) - 2, 0, 4, h);
    g.fillStyle = "#f43f5e";
    g.fillRect(x(end, w) - 2, 0, 4, h);
  }, [wf.data, words, start, end]);

  const msAt = (ev: MouseEvent<HTMLCanvasElement>) => {
    const r = ev.currentTarget.getBoundingClientRect();
    return Math.round(view.from + ((ev.clientX - r.left) / r.width) * span);
  };
  const down = (ev: MouseEvent<HTMLCanvasElement>) => {
    const ms = msAt(ev);
    drag.current = Math.abs(ms - start) < Math.abs(ms - end) ? "start" : "end";
  };
  const move = (ev: MouseEvent<HTMLCanvasElement>) => {
    if (!drag.current) return;
    const ms = msAt(ev);
    if (drag.current === "start") onChange(Math.min(ms, end - 1000), end); else onChange(start, Math.max(ms, start + 1000));
  };

  return (
    <div className="space-y-2 text-xs">
      <canvas ref={canvas} className="h-[90px] w-full cursor-ew-resize rounded bg-zinc-950" onMouseDown={down} onMouseMove={move}
        onMouseUp={() => (drag.current = null)} onMouseLeave={() => (drag.current = null)} />
      <div className="flex flex-wrap items-center gap-2 text-zinc-400">
        <span className="text-emerald-400">in {fmt(start)}</span>
        <button className={ghost} onClick={() => onChange(start - 500, end)}>−0.5s</button>
        <button className={ghost} onClick={() => onChange(Math.min(start + 500, end - 1000), end)}>+0.5s</button>
        <span className="ml-3 text-rose-400">out {fmt(end)}</span>
        <button className={ghost} onClick={() => onChange(start, Math.max(end - 500, start + 1000))}>−0.5s</button>
        <button className={ghost} onClick={() => onChange(start, end + 500)}>+0.5s</button>
        <span className="ml-3">{((end - start) / 1000).toFixed(1)} s · drag the green/red handles, or click a word below</span>
      </div>
      <div className="flex max-h-32 flex-wrap gap-1 overflow-auto rounded bg-zinc-950 p-2">
        {words.length === 0 && <span className="text-zinc-500">No speech here.</span>}
        {words.map((wd, i) => {
          const inside = wd.s >= start && wd.e <= end;
          return (
            <button key={i} title={`${fmt(wd.s)}: click = start here, shift+click = end here`}
              className={`rounded px-1 ${inside ? "bg-zinc-700 text-zinc-100" : "text-zinc-500 hover:text-zinc-300"}`}
              onClick={(ev) => (ev.shiftKey ? onChange(start, Math.max(wd.e + 300, start + 1000)) : onChange(Math.max(0, wd.s - 150), end))}>
              {wd.w}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Versions({ clipId }: { clipId: string }) {
  const versions = useQuery({ queryKey: ["versions", clipId], queryFn: () => apiGet<Version[]>(`/clips/${clipId}/versions`) });
  return (
    <ul className="space-y-1 text-xs">
      {(versions.data ?? []).map((v) => (
        <li key={v.id} className="flex items-center gap-2 rounded bg-zinc-900 p-2">
          <span className="font-medium">v{v.version}</span><span className="text-zinc-400">{v.preset}</span>
          <span className="text-zinc-500">{new Date(v.created_at).toLocaleString()}</span><span>{v.status}</span>
          {v.status === "ready" && (
            <span className="ml-auto flex gap-2">
              <a className={ghost} href={`/api/v1/clip-versions/${v.id}/video`} target="_blank" rel="noreferrer">Play</a>
              <a className={ghost} href={`/api/v1/clip-versions/${v.id}/video`} download>MP4</a>
              <a className={ghost} href={`/api/v1/clip-versions/${v.id}/subtitles.srt`} download>SRT</a>
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function History({ clipId }: { clipId: string }) {
  const log = useQuery({ queryKey: ["audit", clipId], queryFn: () => apiGet<Audit[]>(`/audit?entity_id=${clipId}`) });
  return (
    <ul className="space-y-1 text-xs">
      {(log.data ?? []).map((a, i) => (
        <li key={i} className="flex gap-2"><span className="w-40 text-zinc-500">{new Date(a.at).toLocaleString()}</span>
          <span className="font-medium">{a.action.replace("clip.", "")}</span>
          <span className="truncate text-zinc-400">{Object.entries(a.data).map(([k, v]) => `${k}: ${String(v)}`).join(", ")}</span></li>
      ))}
    </ul>
  );
}
