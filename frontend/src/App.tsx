import { useQuery } from "@tanstack/react-query";
import { ApiError, apiGet, type Health, type Ready } from "./api/client";

function StatusDot({ ok }: { ok: boolean | undefined }) {
  const color = ok === undefined ? "bg-zinc-500" : ok ? "bg-emerald-500" : "bg-rose-500";
  return <span className={`inline-block h-2.5 w-2.5 rounded-full ${color}`} />;
}

export default function App() {
  const health = useQuery({ queryKey: ["health"], queryFn: () => apiGet<Health>("/health") });
  const ready = useQuery({
    queryKey: ["ready"],
    // /ready answers 503 with a useful body when a dependency is down
    queryFn: () =>
      apiGet<Ready>("/ready").catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 503) return e.body as Ready;
        throw e;
      }),
    refetchInterval: 5000,
  });

  return (
    <main className="min-h-screen bg-zinc-950 p-8 text-zinc-100">
      <h1 className="text-2xl font-semibold">ClipForge</h1>
      <p className="mt-1 text-sm text-zinc-400">Phase 0 — system status</p>

      <section className="mt-6 max-w-md space-y-2 rounded-lg border border-zinc-800 p-4">
        <div className="flex items-center gap-2">
          <StatusDot ok={health.isError ? false : health.data ? true : undefined} />
          <span>API</span>
          <span className="ml-auto text-sm text-zinc-400">{health.data?.version ?? (health.isError ? "unreachable" : "…")}</span>
        </div>
        {Object.entries(ready.data?.checks ?? {}).map(([name, state]) => (
          <div key={name} className="flex items-center gap-2">
            <StatusDot ok={state === "ok"} />
            <span className="capitalize">{name}</span>
          </div>
        ))}
      </section>
    </main>
  );
}
