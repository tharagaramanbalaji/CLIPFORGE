"""Host-side bridge: lets the Linux workers use Claude Code (headless, `claude -p`) with the owner's
plan login.

Run on Windows, in the clipforge folder (standard library only, any Python 3.10+):
    python backend/scripts/llm_bridge.py

Safety / cost:
- Refuses to start if ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is set: that would bill the paid API per
  token. Only the subscription login (`claude` -> "Claude account with subscription") is used.
- Every request needs the shared secret CLIPFORGE_LLM_BRIDGE_TOKEN (read from .env).
- At most MAX_CONCURRENT Claude processes at once; Claude Code runs in an empty scratch folder with tools
  disabled, so it only reads the prompt and answers.
- A plan usage-limit message is returned as HTTP 429 so the pipeline retries later instead of failing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # clipforge/
HOST = os.environ.get("CLIPFORGE_LLM_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLIPFORGE_LLM_BRIDGE_PORT", "8765"))
MAX_CONCURRENT = int(os.environ.get("CLIPFORGE_LLM_BRIDGE_CONCURRENCY", "2"))
TIMEOUT_S = 600
LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit", "too many requests", "429")


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


ENV = read_env_file(ROOT / ".env")
TOKEN = os.environ.get("CLIPFORGE_LLM_BRIDGE_TOKEN") or ENV.get("CLIPFORGE_LLM_BRIDGE_TOKEN", "")


def find_claude_exe() -> str:
    """The real executable, never the npm `.cmd` shim: arguments to a .cmd are re-parsed by cmd.exe, which
    mangles prompts containing | & % ^ < > or newlines."""
    override = os.environ.get("CLIPFORGE_CLAUDE_EXE")
    if override:
        return override
    found = shutil.which("claude") or ""
    if found.lower().endswith(".exe"):
        return found
    roots = [Path(found).parent] if found else []
    roots.append(Path(os.environ.get("APPDATA", "")) / "npm")
    for root in roots:
        exe = root / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
    return ""


CLAUDE = find_claude_exe()
SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
WORKDIR = Path(tempfile.mkdtemp(prefix="clipforge-llm-"))  # empty folder: no project files or CLAUDE.md


def run_claude(system: str, user: str, model: str) -> tuple[int, dict[str, object]]:
    # No tools, no MCP servers, empty working folder: Claude Code only reads the prompt and answers.
    args = [
        CLAUDE,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--system-prompt",
        system,
        "--tools",
        "",
        "--strict-mcp-config",
    ]
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    t0 = time.monotonic()
    proc = subprocess.run(
        args,
        input=user,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
        cwd=WORKDIR,
        env=env,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    out = proc.stdout.strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {}
    text = str(data.get("result", "")) if isinstance(data, dict) else ""
    blob = (text + proc.stderr + out[:500]).lower()
    if proc.returncode != 0 or data.get("is_error"):
        status = 429 if any(m in blob for m in LIMIT_MARKERS) else 502
        return status, {"error": (proc.stderr or text or out)[-800:], "returncode": proc.returncode}
    used = list((data.get("modelUsage") or {}).keys())  # resolved model id(s), e.g. claude-sonnet-5-5
    return 200, {
        "text": text,
        "model": used[0] if used else model,
        "usage": data.get("usage") or {},
        "cost_usd": data.get("total_cost_usd"),
        "duration_ms": data.get("duration_ms", elapsed),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "clipforge-llm-bridge/1"

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "claude": bool(CLAUDE), "slots": MAX_CONCURRENT})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/complete":
            return self._send(404, {"error": "not found"})
        if not TOKEN or self.headers.get("X-Bridge-Token") != TOKEN:
            return self._send(401, {"error": "bad or missing X-Bridge-Token"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            system, user, model = str(req["system"]), str(req["user"]), str(req.get("model") or "sonnet")
        except (ValueError, KeyError):
            return self._send(400, {"error": "expected JSON {system, user, model}"})
        with SLOTS:
            try:
                status, payload = run_claude(system, user, model)
            except subprocess.TimeoutExpired:
                status, payload = 504, {"error": f"claude timed out after {TIMEOUT_S}s"}
        self.log_message("complete model=%s status=%s in=%d chars", model, status, len(system) + len(user))
        self._send(status, payload)


def main() -> int:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "Refusing to start: ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN is set, which would bill the "
            "paid API.\n"
            "Unset it so Claude Code uses your plan login.",
            file=sys.stderr,
        )
        return 2
    if CLAUDE.lower().endswith((".cmd", ".bat")):
        print(f"Refusing to use {CLAUDE}: set CLIPFORGE_CLAUDE_EXE to the real claude.exe.", file=sys.stderr)
        return 2
    if not CLAUDE:
        print(
            "Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code, then run `claude`"
            " once and sign in with your Claude account.",
            file=sys.stderr,
        )
        return 2
    if not TOKEN:
        print("CLIPFORGE_LLM_BRIDGE_TOKEN is missing from .env.", file=sys.stderr)
        return 2
    print(
        f"LLM bridge on http://{HOST}:{PORT} using {CLAUDE} (max {MAX_CONCURRENT} at once). Ctrl+C to stop."
    )
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
