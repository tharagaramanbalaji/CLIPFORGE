import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  ApiError,
  apiGet,
  apiSend,
  type Creator,
  type Grant,
  type Health,
  type Me,
  type Media,
  type Project,
  type Ready,
  type RightsBasis,
} from "./api/client";
import { ClipsPanel } from "./Clips";
import { ReviewPage } from "./Review";
import { uploadFile, type Progress } from "./upload";

// ---------- small UI pieces ----------

const input = "w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-sm outline-none focus:border-zinc-400";
const button =
  "rounded bg-zinc-100 px-3 py-1.5 text-sm font-medium text-zinc-900 hover:bg-white disabled:cursor-not-allowed disabled:opacity-40";
const ghost = "rounded border border-zinc-700 px-3 py-1.5 text-sm hover:border-zinc-400 disabled:opacity-40";

function StatusDot({ ok }: { ok: boolean | undefined }) {
  const color = ok === undefined ? "bg-zinc-500" : ok ? "bg-emerald-500" : "bg-rose-500";
  return <span className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${color}`} />;
}

function Card({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="rounded-lg border border-zinc-800 p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="font-semibold">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="mt-2 text-sm text-rose-400">{error instanceof Error ? error.message : String(error)}</p>;
}

const fmtBytes = (n: number) =>
  n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(1)} MB` : `${n} B`;
const fmtDuration = (ms: number | null) => {
  if (ms == null) return "–";
  const s = Math.round(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h ? `${h}:` : ""}${String(m).padStart(h ? 2 : 1, "0")}:${String(s % 60).padStart(2, "0")}`;
};

// ---------- live updates (SSE) ----------

function useLiveEvents(enabled: boolean) {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    const es = new EventSource("/api/v1/events/stream", { withCredentials: true });
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = () => void qc.invalidateQueries({ queryKey: ["media"] });
    return () => es.close();
  }, [enabled, qc]);
  return connected;
}

// ---------- sign in ----------

function SignIn() {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [form, setForm] = useState({ email: "", password: "", display_name: "" });
  const submit = useMutation({
    mutationFn: () =>
      mode === "login"
        ? apiSend("POST", "/auth/login", { email: form.email, password: form.password })
        : apiSend("POST", "/auth/register", form),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit.mutate();
  };
  return (
    <Card title={mode === "login" ? "Sign in" : "Create the owner account"}>
      <form className="max-w-sm space-y-2" onSubmit={onSubmit}>
        {mode === "register" && (
          <input className={input} placeholder="Your name" required value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
        )}
        <input className={input} type="email" placeholder="Email" required autoComplete="username" value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input className={input} type="password" placeholder={mode === "register" ? "Password (10+ characters)" : "Password"}
          required minLength={mode === "register" ? 10 : 1} value={form.password}
          autoComplete={mode === "register" ? "new-password" : "current-password"}
          onChange={(e) => setForm({ ...form, password: e.target.value })} />
        <div className="flex items-center gap-3">
          <button className={button} disabled={submit.isPending}>{mode === "login" ? "Sign in" : "Create account"}</button>
          <button type="button" className="text-sm text-zinc-400 underline"
            onClick={() => setMode(mode === "login" ? "register" : "login")}>
            {mode === "login" ? "First time? Create the owner account" : "Already set up? Sign in"}
          </button>
        </div>
        <ErrorText error={submit.error} />
      </form>
    </Card>
  );
}

// ---------- creators, rights, projects ----------

const BASIS_LABEL: Record<RightsBasis, string> = {
  own_content: "My own content",
  written_permission: "Written permission",
  license_agreement: "License agreement",
  platform_authorized_integration: "Platform-authorized integration",
};

function CreatorsAndRights({ creators, onPick, picked }: { creators: Creator[]; picked: string; onPick: (id: string) => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [grant, setGrant] = useState<{ basis: RightsBasis; note: string; until: string; music: boolean }>({
    basis: "written_permission", note: "", until: "", music: false,
  });
  const grants = useQuery({
    queryKey: ["grants", picked],
    queryFn: () => apiGet<Grant[]>(`/creators/${picked}/rights-grants`),
    enabled: !!picked,
  });
  const addCreator = useMutation({
    mutationFn: () => apiSend<Creator>("POST", "/creators", { name }),
    onSuccess: (c) => {
      setName("");
      onPick(c.id);
      return qc.invalidateQueries({ queryKey: ["creators"] });
    },
  });
  const addGrant = useMutation({
    mutationFn: () =>
      apiSend<Grant>("POST", `/creators/${picked}/rights-grants`, {
        basis: grant.basis,
        evidence_note: grant.note || null,
        valid_until: grant.until ? new Date(grant.until).toISOString() : null,
        scope: { contains_copyrighted_music: grant.music },
      }),
    onSuccess: () => {
      setGrant({ ...grant, note: "", until: "" });
      return qc.invalidateQueries({ queryKey: ["grants"] });
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => apiSend("POST", `/rights-grants/${id}/revoke`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["grants"] }),
  });

  return (
    <Card title="1 · Creators & rights">
      <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); addCreator.mutate(); }}>
        <input className={input} placeholder="New creator (streamer/channel name)" value={name} required
          onChange={(e) => setName(e.target.value)} />
        <button className={button} disabled={addCreator.isPending}>Add</button>
      </form>
      <ErrorText error={addCreator.error} />
      <select className={`${input} mt-3`} value={picked} onChange={(e) => onPick(e.target.value)}>
        <option value="">Select a creator…</option>
        {creators.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select>

      {picked && (
        <div className="mt-4 space-y-3">
          <ul className="space-y-1 text-sm">
            {grants.data?.length === 0 && <li className="text-zinc-400">No rights recorded yet — uploads are blocked until you add one.</li>}
            {grants.data?.map((g) => (
              <li key={g.id} className="flex items-center gap-2">
                <StatusDot ok={g.is_active} />
                <span>{BASIS_LABEL[g.basis]}</span>
                <span className="truncate text-zinc-400">
                  {g.revoked_at ? "revoked" : g.valid_until ? `until ${new Date(g.valid_until).toLocaleDateString()}` : "no expiry"}
                  {g.evidence_note ? ` · ${g.evidence_note}` : ""}
                </span>
                {!g.revoked_at && (
                  <button className="ml-auto text-xs text-zinc-400 underline" onClick={() => revoke.mutate(g.id)}>revoke</button>
                )}
              </li>
            ))}
          </ul>
          <form className="space-y-2 rounded border border-zinc-800 p-3" onSubmit={(e) => { e.preventDefault(); addGrant.mutate(); }}>
            <p className="text-sm text-zinc-300">Record permission to clip this creator</p>
            <select className={input} value={grant.basis} onChange={(e) => setGrant({ ...grant, basis: e.target.value as RightsBasis })}>
              {Object.entries(BASIS_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            <input className={input} placeholder="Evidence (where/when permission was given)" value={grant.note}
              onChange={(e) => setGrant({ ...grant, note: e.target.value })} />
            <label className="flex items-center gap-2 text-sm text-zinc-400">
              Expires <input className={`${input} w-auto`} type="date" value={grant.until}
                onChange={(e) => setGrant({ ...grant, until: e.target.value })} />
            </label>
            <label className="flex items-center gap-2 text-sm text-zinc-400">
              <input type="checkbox" checked={grant.music} onChange={(e) => setGrant({ ...grant, music: e.target.checked })} />
              Source often contains copyrighted music
            </label>
            <button className={button} disabled={addGrant.isPending}>Save rights grant</button>
            <ErrorText error={addGrant.error ?? revoke.error} />
          </form>
        </div>
      )}
    </Card>
  );
}

function Projects({ creators, projects, picked, onPick }: { creators: Creator[]; projects: Project[]; picked: string; onPick: (id: string) => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [creatorId, setCreatorId] = useState("");
  const add = useMutation({
    mutationFn: () => apiSend<Project>("POST", "/projects", { name, creator_id: creatorId }),
    onSuccess: (p) => {
      setName("");
      onPick(p.id);
      return qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });
  const creatorName = (id: string) => creators.find((c) => c.id === id)?.name ?? "?";
  return (
    <Card title="2 · Project">
      <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
        <input className={input} placeholder="New project name" value={name} required onChange={(e) => setName(e.target.value)} />
        <div className="flex gap-2">
          <select className={input} value={creatorId} required onChange={(e) => setCreatorId(e.target.value)}>
            <option value="">for creator…</option>
            {creators.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <button className={button} disabled={add.isPending}>Create</button>
        </div>
      </form>
      <ErrorText error={add.error} />
      <select className={`${input} mt-3`} value={picked} onChange={(e) => onPick(e.target.value)}>
        <option value="">Select a project…</option>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.name} — {creatorName(p.creator_id)}</option>)}
      </select>
    </Card>
  );
}

// ---------- upload ----------

function Uploader({ project }: { project: Project }) {
  const qc = useQueryClient();
  const grants = useQuery({
    queryKey: ["grants", project.creator_id],
    queryFn: () => apiGet<Grant[]>(`/creators/${project.creator_id}/rights-grants`),
  });
  const active = (grants.data ?? []).filter((g) => g.is_active);
  const [grantId, setGrantId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [state, setState] = useState<"idle" | "uploading" | "paused" | "done" | "error">("idle");
  const [error, setError] = useState<unknown>(null);
  const abort = useRef<AbortController | null>(null);
  const started = useRef(0);

  useEffect(() => {
    if (!grantId && active.length) setGrantId(active[0].id);
  }, [active, grantId]);

  const start = async () => {
    if (!file || !grantId) return;
    abort.current = new AbortController();
    started.current = performance.now();
    setState("uploading");
    setError(null);
    try {
      await uploadFile(project.id, grantId, file, setProgress, abort.current.signal);
      setState("done");
      void qc.invalidateQueries({ queryKey: ["media"] });
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") setState("paused");
      else {
        setState("error");
        setError(e instanceof ApiError || e instanceof Error ? e : String(e));
      }
    }
  };

  const pct = progress ? (100 * progress.sentBytes) / progress.totalBytes : 0;
  return (
    <Card title="3 · Upload a video">
      {grants.isSuccess && active.length === 0 ? (
        <p className="text-sm text-amber-400">This project's creator has no active rights grant. Record one in step 1 first.</p>
      ) : (
        <div className="space-y-3">
          <select className={input} value={grantId} onChange={(e) => setGrantId(e.target.value)}>
            {active.map((g) => <option key={g.id} value={g.id}>Rights: {BASIS_LABEL[g.basis]}{g.evidence_note ? ` · ${g.evidence_note}` : ""}</option>)}
          </select>
          <input type="file" accept="video/*,.mkv,.flv,.ts" className="block w-full text-sm text-zinc-300 file:mr-3 file:rounded file:border-0 file:bg-zinc-800 file:px-3 file:py-1.5 file:text-zinc-100"
            disabled={state === "uploading"}
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setProgress(null); setState("idle"); }} />
          {file && <p className="text-sm text-zinc-400">{file.name} · {fmtBytes(file.size)}</p>}
          <div className="flex gap-2">
            {state === "uploading" ? (
              <button className={ghost} onClick={() => abort.current?.abort()}>Pause</button>
            ) : (
              <button className={button} disabled={!file || !grantId} onClick={() => void start()}>
                {state === "paused" || state === "error" ? "Resume" : "Upload"}
              </button>
            )}
          </div>
          {progress && (
            <div>
              <div className="h-2 overflow-hidden rounded bg-zinc-800">
                <div className="h-full bg-emerald-500 transition-all" style={{ width: `${pct}%` }} />
              </div>
              <p className="mt-1 text-xs text-zinc-400">
                {fmtBytes(progress.sentBytes)} / {fmtBytes(progress.totalBytes)} ({pct.toFixed(1)}%)
                {state === "done" && " · uploaded, processing below"}
                {state === "paused" && " · paused — Resume continues where it stopped, even after a reload"}
              </p>
            </div>
          )}
          <ErrorText error={error} />
        </div>
      )}
    </Card>
  );
}

// ---------- media list ----------

const STATUS_COLOR: Record<string, string> = {
  ready: "text-emerald-400", failed: "text-rose-400", uploading: "text-zinc-400", validating: "text-amber-300", uploaded: "text-amber-300",
};

function MediaList({ project }: { project: Project }) {
  const qc = useQueryClient();
  const [reviewing, setReviewing] = useState<Media | null>(null);
  const media = useQuery({
    queryKey: ["media", project.id],
    queryFn: () => apiGet<Media[]>(`/projects/${project.id}/media`),
    refetchInterval: 15000,
  });
  const action = useMutation({
    mutationFn: ({ path, method }: { path: string; method: "POST" | "DELETE" }) => apiSend(method, path),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["media"] }),
  });
  if (reviewing) {
    return <ReviewPage mediaId={reviewing.id} name={reviewing.original_filename} onClose={() => setReviewing(null)} />;
  }
  return (
    <Card title="Media in this project">
      {media.data?.length === 0 && <p className="text-sm text-zinc-400">Nothing uploaded yet.</p>}
      <ul className="space-y-3">
        {media.data?.map((m) => {
          const run = m.latest_run;
          return (
            <li key={m.id} className="rounded border border-zinc-800 p-3">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-medium break-all">{m.original_filename}</span>
                <span className={`text-sm ${STATUS_COLOR[m.status] ?? ""}`}>{m.status}</span>
                <span className="ml-auto flex gap-3 text-xs">
                  {m.status === "ready" && (
                    <button className="rounded bg-zinc-100 px-2 py-0.5 font-medium text-zinc-900" onClick={() => setReviewing(m)}>Review</button>
                  )}
                  {run && (run.status === "failed" || run.status === "canceled") && (
                    <button className="underline" onClick={() => action.mutate({ path: `/pipeline-runs/${run.id}/retry`, method: "POST" })}>retry</button>
                  )}
                  {run && run.status === "running" && (
                    <button className="underline" onClick={() => action.mutate({ path: `/pipeline-runs/${run.id}/cancel`, method: "POST" })}>cancel</button>
                  )}
                  <button className="text-zinc-400 underline" onClick={() => action.mutate({ path: `/media/${m.id}`, method: "DELETE" })}>archive</button>
                </span>
              </div>
              <p className="mt-1 text-xs text-zinc-400">
                {fmtBytes(m.size_bytes)} · {fmtDuration(m.duration_ms)}
                {m.width ? ` · ${m.width}×${m.height}` : ""}
                {m.fps_num && m.fps_den ? ` · ${(m.fps_num / m.fps_den).toFixed(2)} fps${m.is_vfr ? " (variable → normalized)" : ""}` : ""}
                {m.video_codec ? ` · ${m.video_codec}/${m.audio_codec ?? "no audio"}` : ""}
              </p>
              {m.error && <p className="mt-1 text-sm text-rose-400">{m.error.message}</p>}
              {run && (
                <ol className="mt-2 flex flex-wrap gap-2 text-xs">
                  {run.stages.map((s) => (
                    <li key={s.stage} className="flex items-center gap-1.5 rounded bg-zinc-900 px-2 py-1">
                      <StatusDot ok={s.status === "succeeded" || s.status === "skipped" ? true : s.status === "failed" ? false : undefined} />
                      {s.stage}
                      <span className="text-zinc-500">
                        {s.status === "running" ? `${Math.round(s.progress * 100)}%` : s.status}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
              {m.status === "ready" && <ClipsPanel mediaId={m.id} />}
            </li>
          );
        })}
      </ul>
      <ErrorText error={media.error ?? action.error} />
    </Card>
  );
}

// ---------- page ----------

export default function App() {
  const qc = useQueryClient();
  const health = useQuery({ queryKey: ["health"], queryFn: () => apiGet<Health>("/health") });
  const ready = useQuery({
    queryKey: ["ready"],
    queryFn: () =>
      apiGet<Ready>("/ready").catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 503) return e.body as Ready;
        throw e;
      }),
    refetchInterval: 10000,
  });
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => apiGet<Me>("/auth/me").catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return null;
      throw e;
    }),
  });
  const signedIn = !!me.data;
  const live = useLiveEvents(signedIn);
  const creators = useQuery({ queryKey: ["creators"], queryFn: () => apiGet<Creator[]>("/creators"), enabled: signedIn });
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => apiGet<Project[]>("/projects"), enabled: signedIn });
  const [creatorId, setCreatorId] = useState("");
  const [projectId, setProjectId] = useState("");
  const project = projects.data?.find((p) => p.id === projectId);

  const logout = async () => {
    await apiSend("POST", "/auth/logout");
    qc.clear();
  };

  return (
    <main className="min-h-screen bg-zinc-950 px-4 py-6 text-zinc-100 sm:px-8">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="text-2xl font-semibold">ClipForge</h1>
        <span className="flex items-center gap-1.5 text-xs text-zinc-400"><StatusDot ok={health.isError ? false : health.data ? true : undefined} />API {health.data?.version ?? ""}</span>
        {Object.entries(ready.data?.checks ?? {}).map(([name, s]) => (
          <span key={name} className="flex items-center gap-1.5 text-xs capitalize text-zinc-400"><StatusDot ok={s === "ok"} />{name}</span>
        ))}
        {signedIn && <span className="flex items-center gap-1.5 text-xs text-zinc-400"><StatusDot ok={live} />live updates</span>}
        {signedIn && (
          <span className="ml-auto flex items-center gap-3 text-sm text-zinc-400">
            {me.data?.display_name}
            <button className="underline" onClick={() => void logout()}>sign out</button>
          </span>
        )}
      </header>

      <div className="mt-6 grid max-w-5xl gap-4 md:grid-cols-2">
        {me.isSuccess && !signedIn && <SignIn />}
        {signedIn && (
          <>
            <CreatorsAndRights creators={creators.data ?? []} picked={creatorId} onPick={setCreatorId} />
            <Projects creators={creators.data ?? []} projects={projects.data ?? []} picked={projectId} onPick={setProjectId} />
            {project && <div className="md:col-span-2"><Uploader key={project.id} project={project} /></div>}
            {project && <div className="md:col-span-2"><MediaList project={project} /></div>}
          </>
        )}
      </div>
    </main>
  );
}
