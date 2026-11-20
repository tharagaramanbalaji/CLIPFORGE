// Minimal clip workflow for one media: find highlights -> make clips -> render -> watch.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, apiGet, apiSend } from "./api/client";

type Candidate = {
  id: string;
  rank: number | null;
  final_score: number | null;
  start_ms: number;
  end_ms: number;
  category: string;
  title: string | null;
  hook: string;
  extra: { source?: string; judge_rejected?: boolean };
};
type Version = { id: string; version: number; preset: string; status: string; error: { message: string } | null };
type Clip = {
  id: string;
  title: string;
  start_ms: number;
  end_ms: number;
  status: string;
  candidate_id: string | null;
  latest_version: Version | null;
};
type Word = { w: string; s: number; e: number };
type Subs = { edited: boolean; words: Word[]; captions: { start_ms: number; end_ms: number; text: string }[] };
type Estimate = { requests: number; input_tokens: number; signal_moments: number };

const btn = "rounded bg-zinc-100 px-2.5 py-1 text-xs font-medium text-zinc-900 hover:bg-white disabled:opacity-40";
const ghost = "rounded border border-zinc-700 px-2.5 py-1 text-xs hover:border-zinc-400 disabled:opacity-40";
const t = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor((ms % 60000) / 1000)).padStart(2, "0")}`;
const errText = (e: unknown) => (e instanceof Error ? e.message : e ? String(e) : "");

export function ClipsPanel({ mediaId }: { mediaId: string }) {
  const qc = useQueryClient();
  const [content, setContent] = useState<"talk" | "gaming">("gaming");
  const [preset, setPreset] = useState("shorts");
  const [mode, setMode] = useState("auto");
  const [count, setCount] = useState(5);
  const [corner, setCorner] = useState("top_right");
  const [below, setBelow] = useState(true);
  const [srcPath, setSrcPath] = useState("");
  const [playing, setPlaying] = useState<string | null>(null);
  const [subStyle, setSubStyle] = useState("bold_pop");
  const [hook, setHook] = useState(true);
  const [watermark, setWatermark] = useState("");
  const [editing, setEditing] = useState<string | null>(null);

  const candidates = useQuery({
    queryKey: ["media", mediaId, "candidates"],
    queryFn: () =>
      apiGet<Candidate[]>(`/media/${mediaId}/candidates`).catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 404) return [] as Candidate[];
        throw e;
      }),
  });
  const clips = useQuery({ queryKey: ["media", mediaId, "clips"], queryFn: () => apiGet<Clip[]>(`/media/${mediaId}/clips`) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["media", mediaId] });

  const estimate = useMutation({
    mutationFn: () => apiSend<Estimate>("POST", `/media/${mediaId}/highlights?dry_run=true&content_type=${content}`),
  });
  const find = useMutation({
    mutationFn: () => apiSend("POST", `/media/${mediaId}/highlights?content_type=${content}`),
    onSuccess: refresh,
  });
  const makeClip = useMutation({
    mutationFn: (candidateId: string) => apiSend("POST", `/media/${mediaId}/clips`, { candidate_ids: [candidateId] }),
    onSuccess: refresh,
  });
  const autoEdit = useMutation({
    mutationFn: () =>
      apiSend("POST", `/media/${mediaId}/auto-edit`, {
        count, preset, subtitle_style: subStyle, hook_text: hook, watermark: watermark || null,
      }),
    onSuccess: refresh,
  });
  const render = useMutation({
    mutationFn: (clipId: string) =>
      apiSend("POST", `/clips/${clipId}/render`, {
        preset, frame_mode: mode, subtitle_style: subStyle, hook_text: hook, watermark: watermark || null,
        captions_below: below, facecam: mode === "facecam_split" ? corner : null,
      }),
    onSuccess: refresh,
  });
  const clipped = new Set((clips.data ?? []).map((c) => c.candidate_id));

  return (
    <div className="mt-3 space-y-3 border-t border-zinc-800 pt-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-zinc-400">Highlights for</span>
        <select className="rounded bg-zinc-900 px-1.5 py-1" value={content} onChange={(e) => setContent(e.target.value as "talk" | "gaming")}>
          <option value="gaming">gaming</option>
          <option value="talk">talk / podcast</option>
        </select>
        <button className={ghost} onClick={() => estimate.mutate()}>Estimate usage</button>
        <button className={btn} disabled={find.isPending} onClick={() => find.mutate()}>Find highlights (uses Claude)</button>
        {estimate.data && (
          <span className="text-zinc-400">
            ≈ {estimate.data.requests} requests, {Math.round(estimate.data.input_tokens / 1000)}k tokens
          </span>
        )}
      </div>
      <p className="text-xs text-rose-400">{errText(estimate.error ?? find.error ?? makeClip.error ?? render.error)}</p>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-zinc-400">Premiere</span>
        <input className="w-72 rounded bg-zinc-900 px-1.5 py-1" placeholder="optional: original file path on this PC, e.g. C:\Videos\stream.mp4"
          value={srcPath} onChange={(e) => setSrcPath(e.target.value)} />
        <a className={ghost} href={`/api/v1/media/${mediaId}/export/premiere${srcPath ? `?source_path=${encodeURIComponent(srcPath)}` : ""}`} download>
          Export to Premiere
        </a>
      </div>

      {(candidates.data?.length ?? 0) > 0 && (
        <div className="flex items-center gap-2 text-xs">
          <button className={btn} disabled={autoEdit.isPending} onClick={() => autoEdit.mutate()}>
            Auto-edit top
          </button>
          <input type="number" min={1} max={20} className="w-14 rounded bg-zinc-900 px-1.5 py-1" value={count}
            onChange={(e) => setCount(Number(e.target.value))} />
          <span className="text-zinc-400">clips: facecam on top when visible, captions, hook, watermark (options below)</span>
          <span className="text-rose-400">{errText(autoEdit.error)}</span>
        </div>
      )}
      {(candidates.data?.length ?? 0) > 0 && (
        <ol className="space-y-1 text-xs">
          {candidates.data!.map((c) => (
            <li key={c.id} className="flex items-center gap-2">
              <span className="w-6 text-zinc-500">#{c.rank}</span>
              <span className="w-24 tabular-nums text-zinc-400">{t(c.start_ms)}–{t(c.end_ms)}</span>
              <span className="flex-1 truncate">{c.title ?? c.hook}</span>
              <span className="text-zinc-500">{c.category}{c.extra.source === "signal" ? " · signal" : ""}</span>
              <button className={ghost} disabled={clipped.has(c.id) || makeClip.isPending} onClick={() => makeClip.mutate(c.id)}>
                {clipped.has(c.id) ? "clipped" : "Make clip"}
              </button>
            </li>
          ))}
        </ol>
      )}

      {(clips.data?.length ?? 0) > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs">
            <span className="text-zinc-400">Render as</span>
            <select className="rounded bg-zinc-900 px-1.5 py-1" value={preset} onChange={(e) => setPreset(e.target.value)}>
              {["shorts", "reels", "tiktok", "square", "wide"].map((p) => <option key={p}>{p}</option>)}
            </select>
            <select className="rounded bg-zinc-900 px-1.5 py-1" value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="auto">auto (facecam on top when visible)</option>
              <option value="blur_pad">fit + blurred background</option>
              <option value="crop">center crop</option>
              <option value="facecam_split">facecam on top + gameplay</option>
              <option value="two_stack">two people stacked</option>
            </select>
            {mode === "facecam_split" && (
              <select className="rounded bg-zinc-900 px-1.5 py-1" value={corner} onChange={(e) => setCorner(e.target.value)}>
                {["top_left", "top_right", "bottom_left", "bottom_right"].map((c) => <option key={c} value={c}>facecam {c.replace("_", " ")}</option>)}
              </select>
            )}
            {mode === "blur_pad" && (
              <label className="flex items-center gap-1"><input type="checkbox" checked={below} onChange={(e) => setBelow(e.target.checked)} />captions under picture</label>
            )}
            <select className="rounded bg-zinc-900 px-1.5 py-1" value={subStyle} onChange={(e) => setSubStyle(e.target.value)}>
              <option value="bold_pop">captions: bold pop</option>
              <option value="clean_box">captions: clean box</option>
              <option value="karaoke">captions: karaoke</option>
              <option value="none">no captions</option>
            </select>
            <label className="flex items-center gap-1"><input type="checkbox" checked={hook} onChange={(e) => setHook(e.target.checked)} />hook text</label>
            <input className="w-28 rounded bg-zinc-900 px-1.5 py-1" placeholder="@watermark" value={watermark} onChange={(e) => setWatermark(e.target.value)} />
          </div>
          <ul className="space-y-2">
            {clips.data!.map((c) => {
              const v = c.latest_version;
              return (
                <li key={c.id} className="rounded bg-zinc-900 p-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{c.title}</span>
                    <span className="tabular-nums text-zinc-400">{t(c.start_ms)}–{t(c.end_ms)}</span>
                    <span className="text-zinc-500">{v ? `v${v.version} ${v.preset} · ${v.status}` : c.status}</span>
                    <span className="ml-auto flex gap-2">
                      {v?.status === "ready" && (
                        <>
                          <button className={ghost} onClick={() => setPlaying(playing === v.id ? null : v.id)}>
                            {playing === v.id ? "Hide" : "Play"}
                          </button>
                          <a className={ghost} href={`/api/v1/clip-versions/${v.id}/video`} download={`${c.title}.mp4`}>Download</a>
                        </>
                      )}
                      {v?.status === "ready" && <a className={ghost} href={`/api/v1/clip-versions/${v.id}/subtitles.srt`} download>SRT</a>}
                      <button className={ghost} onClick={() => setEditing(editing === c.id ? null : c.id)}>Subtitles</button>
                      <button className={btn} disabled={c.status === "rendering"} onClick={() => render.mutate(c.id)}>
                        {v ? "Re-render" : "Render"}
                      </button>
                    </span>
                  </div>
                  {v?.error && <p className="mt-1 text-rose-400">{v.error.message}</p>}
                  {editing === c.id && <SubtitleEditor clipId={c.id} />}
                  {v && playing === v.id && (
                    <video className="mt-2 max-h-[480px] rounded" src={`/api/v1/clip-versions/${v.id}/video`}
                      poster={`/api/v1/clip-versions/${v.id}/thumbnail`} controls autoPlay />
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

// Edit caption text; a changed word count spreads that caption's time evenly over the new words.
function SubtitleEditor({ clipId }: { clipId: string }) {
  const qc = useQueryClient();
  const subs = useQuery({ queryKey: ["subs", clipId], queryFn: () => apiGet<Subs>(`/clips/${clipId}/subtitles`) });
  const [draft, setDraft] = useState<Record<number, string>>({});
  const save = useMutation({
    mutationFn: (words: Word[]) => apiSend<Subs>("PUT", `/clips/${clipId}/subtitles`, { words }),
    onSuccess: () => { setDraft({}); return qc.invalidateQueries({ queryKey: ["subs", clipId] }); },
  });
  const reset = useMutation({
    mutationFn: () => apiSend<Subs>("DELETE", `/clips/${clipId}/subtitles`),
    onSuccess: () => { setDraft({}); return qc.invalidateQueries({ queryKey: ["subs", clipId] }); },
  });
  if (!subs.data) return <p className="mt-2 text-zinc-500">loading subtitles…</p>;
  const { words, captions } = subs.data;

  const build = (): Word[] => {
    const out: Word[] = [];
    captions.forEach((cap, ci) => {
      const own = words.filter((w) => w.s >= cap.start_ms && w.s < cap.end_ms); // words shown in this caption
      const text = draft[ci];
      if (text === undefined) { out.push(...own); return; }
      const toks = text.trim().split(/\s+/).filter(Boolean);
      if (toks.length === own.length) { own.forEach((w, i) => out.push({ ...w, w: toks[i] })); return; }
      const s = own[0]?.s ?? cap.start_ms, e = own[own.length - 1]?.e ?? cap.end_ms, step = (e - s) / Math.max(1, toks.length);
      toks.forEach((t, i) => out.push({ w: t, s: Math.round(s + i * step), e: Math.round(s + (i + 1) * step) }));
    });
    return out;
  };

  return (
    <div className="mt-2 space-y-1">
      {captions.map((cap, ci) => (
        <div key={ci} className="flex items-center gap-2">
          <span className="w-12 text-zinc-500 tabular-nums">{t(cap.start_ms)}</span>
          <input className="flex-1 rounded bg-zinc-950 px-1.5 py-0.5" value={draft[ci] ?? cap.text}
            onChange={(e) => setDraft({ ...draft, [ci]: e.target.value })} />
        </div>
      ))}
      <div className="flex gap-2 pt-1">
        <button className={btn} disabled={!Object.keys(draft).length || save.isPending} onClick={() => save.mutate(build())}>Save edits</button>
        {subs.data.edited && <button className={ghost} onClick={() => reset.mutate()}>Reset to transcript</button>}
        <span className="text-zinc-500">{subs.data.edited ? "edited · " : ""}re-render to apply</span>
      </div>
      <p className="text-rose-400">{errText(save.error ?? reset.error)}</p>
    </div>
  );
}
