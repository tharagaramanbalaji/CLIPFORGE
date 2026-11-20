// Thin fetch wrapper. All calls go through the Vite proxy (/api -> backend); no secrets in the browser.
export const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(errorMessage(body) ?? `API ${status}`);
  }
}

function errorMessage(body: unknown): string | undefined {
  if (body && typeof body === "object") {
    const b = body as { error?: { message?: string }; detail?: unknown };
    if (b.error?.message) return b.error.message;
    if (typeof b.detail === "string") return b.detail;
    if (Array.isArray(b.detail)) return b.detail.map((d: { msg?: string }) => d.msg).join("; ");
  }
  return undefined;
}

async function request<T>(method: string, path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method, credentials: "include", ...init });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, body);
  return body as T;
}

export const apiGet = <T>(path: string) => request<T>("GET", path);

export const apiSend = <T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, json?: unknown) =>
  request<T>(method, path, {
    headers: json === undefined ? undefined : { "Content-Type": "application/json" },
    body: json === undefined ? undefined : JSON.stringify(json),
  });

export const apiPutBytes = <T>(path: string, data: Blob, headers: Record<string, string>, signal?: AbortSignal) =>
  request<T>("PUT", path, { body: data, headers: { "Content-Type": "application/octet-stream", ...headers }, signal });

export type Ready = { status: "ready" | "not_ready"; checks: Record<string, "ok" | "fail"> };
export type Health = { status: "ok"; version: string };
export type Me = { id: string; email: string; display_name: string; workspace_id: string; role: string };
export type Creator = { id: string; name: string; notes: string | null };
export type RightsBasis = "own_content" | "written_permission" | "license_agreement" | "platform_authorized_integration";
export type Grant = {
  id: string;
  creator_id: string;
  basis: RightsBasis;
  valid_from: string;
  valid_until: string | null;
  evidence_note: string | null;
  revoked_at: string | null;
  is_active: boolean;
};
export type Project = { id: string; name: string; creator_id: string; status: string };
export type Upload = {
  upload_id: string;
  media_id: string;
  filename: string;
  total_bytes: number;
  chunk_size: number;
  total_chunks: number;
  received_chunks: number[];
  received_bytes: number;
  complete: boolean;
  expires_at: string;
};
export type Stage = {
  stage: string;
  status: string;
  attempt: number;
  progress: number;
  metrics: Record<string, unknown>;
  error: { message: string } | null;
};
export type Run = { id: string; kind: string; status: string; error: { message: string } | null; stages: Stage[] };
export type Media = {
  id: string;
  original_filename: string;
  status: string;
  size_bytes: number;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  fps_num: number | null;
  fps_den: number | null;
  is_vfr: boolean | null;
  video_codec: string | null;
  audio_codec: string | null;
  container: string | null;
  error: { message: string } | null;
  latest_run: Run | null;
};
