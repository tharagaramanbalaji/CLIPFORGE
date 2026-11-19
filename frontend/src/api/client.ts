// Thin fetch wrapper. All calls go through the Vite proxy (/api -> backend); no secrets in the browser.
export const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
  ) {
    super(`API ${status}`);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { credentials: "include" });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, body);
  return body as T;
}

export type Ready = { status: "ready" | "not_ready"; checks: Record<string, "ok" | "fail"> };
export type Health = { status: "ok"; version: string };
