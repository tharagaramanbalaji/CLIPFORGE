// Resumable chunked uploader. The server is the source of truth for which chunks arrived, so a reload,
// a dropped connection or "Pause" all resume the same way: ask the server, send what is missing.
import { apiGet, apiPutBytes, apiSend, type Upload } from "./api/client";

const PARALLEL = 3;
const MAX_TRIES = 5;

export type Progress = { sentBytes: number; totalBytes: number; uploadId: string };

const storageKey = (projectId: string, f: File) => `clipforge.upload:${projectId}:${f.name}:${f.size}:${f.lastModified}`;

function remember(key: string, uploadId: string | null) {
  try {
    if (uploadId) localStorage.setItem(key, uploadId);
    else localStorage.removeItem(key);
  } catch {
    /* storage unavailable: resume still works within this page */
  }
}

function recall(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

async function sha256Hex(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function openSession(projectId: string, grantId: string, file: File): Promise<Upload> {
  const key = storageKey(projectId, file);
  const existing = recall(key);
  if (existing) {
    try {
      const up = await apiGet<Upload>(`/uploads/${existing}`);
      if (!up.complete && new Date(up.expires_at) > new Date()) return up;
    } catch {
      /* expired or gone: start fresh */
    }
  }
  const up = await apiSend<Upload>("POST", `/projects/${projectId}/uploads`, {
    filename: file.name,
    size_bytes: file.size,
    rights_grant_id: grantId,
  });
  remember(key, up.upload_id);
  return up;
}

export async function uploadFile(
  projectId: string,
  grantId: string,
  file: File,
  onProgress: (p: Progress) => void,
  signal: AbortSignal,
): Promise<{ media_id: string; pipeline_run_id: string }> {
  const up = await openSession(projectId, grantId, file);
  const done = new Set(up.received_chunks);
  const sizeOf = (n: number) => Math.min(up.chunk_size, up.total_bytes - n * up.chunk_size);
  let sent = [...done].reduce((s, n) => s + sizeOf(n), 0);
  onProgress({ sentBytes: sent, totalBytes: up.total_bytes, uploadId: up.upload_id });

  const todo = Array.from({ length: up.total_chunks }, (_, n) => n).filter((n) => !done.has(n));
  const worker = async () => {
    for (let n = todo.shift(); n !== undefined; n = todo.shift()) {
      const blob = file.slice(n * up.chunk_size, n * up.chunk_size + sizeOf(n));
      const hash = await sha256Hex(blob);
      for (let attempt = 1; ; attempt++) {
        if (signal.aborted) throw new DOMException("Paused", "AbortError");
        try {
          await apiPutBytes(`/uploads/${up.upload_id}/chunks/${n}`, blob, { "X-Chunk-SHA256": hash }, signal);
          break;
        } catch (e) {
          if (signal.aborted || attempt >= MAX_TRIES) throw e;
          await sleep(1000 * 2 ** attempt);
        }
      }
      sent += blob.size;
      onProgress({ sentBytes: sent, totalBytes: up.total_bytes, uploadId: up.upload_id });
    }
  };
  await Promise.all(Array.from({ length: PARALLEL }, worker));

  const result = await apiSend<{ media_id: string; pipeline_run_id: string }>("POST", `/uploads/${up.upload_id}/complete`);
  remember(storageKey(projectId, file), null);
  return result;
}
