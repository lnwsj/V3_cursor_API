"""Compact PWA + SSE local dashboard server for the bigger-stats design.

Serves on http://127.0.0.1:8788 (configurable) and includes:

  - ``/``              -- live SSE-driven single-file HTML dashboard
  - ``/style.css``     -- dark theme (GitHub-ish)
  - ``/live.js``       -- JS client; subscribes to /api/stream and re-renders
  - ``/manifest.webmanifest`` -- PWA manifest (installable as standalone app)
  - ``/service-worker.js``   -- offline cache (cache-first strategy)
  - ``/icon-192.svg``   -- PWA app icon (SJ monogram, dark theme)
  - ``/api/stats.json`` -- full dashboard JSON dump (no SSE)
  - ``/api/stream``     -- Server-Sent Events: pushes /api/stats.json every 2 s
  - ``/api/seeder/preview`` -- inject 12 sample events into outbox so the
                            dashboard renders something on first run
  - ``/healthz``        -- liveness probe

Reads the outbox + (optionally) fetches public data from
greenstats.sj88ai.com.  Designed to be both:
  - useful offline (uses outbox only)
  - useful online (merges live public + local outbox)
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional


DEFAULT_OUTBOX = Path.home() / ".green_pc" / "usage_stats_outbox.sqlite3"
PUBLIC_BASE = "https://greenstats.sj88ai.com/api/v1"
FETCH_TIMEOUT = 1.5


def _read_outbox_rows(db_path: Path) -> List[Dict[str, Any]]:
    """Read all rows from the outbox + quarantine tables."""
    if not db_path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(str(db_path))
    except Exception:
        return []
    quarantine = {
        row[0] for row in conn.execute("SELECT event_id FROM quarantine")
    }
    for row in conn.execute(
        "SELECT event_id, payload_json, attempts FROM outbox ORDER BY created_at DESC"
    ):
        try:
            payload = json.loads(row[1] or "{}")
        except Exception:
            payload = {}
        attempts = int(row[2] or 0)
        if row[0] in quarantine:
            status = "quarantined"
        elif attempts > 0:
            status = "failed"
        else:
            status = "pending"
        out.append({
            "event_id": row[0],
            "payload": payload,
            "status": status,
            "attempts": attempts,
        })
    conn.close()
    return out


def _safe_fetch_json(path: str, timeout: float = FETCH_TIMEOUT) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"{PUBLIC_BASE}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _p95(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, int(round(0.95 * (len(s) - 1))))
    return s[k]


class DashboardBuilder:
    def __init__(self, *, outbox: Path, fetch_remote: bool):
        self.outbox = outbox
        self.fetch_remote = fetch_remote
        self.local_rows = _read_outbox_rows(outbox)
        self.remote: Dict[str, Any] = {}

    def build(self) -> Dict[str, Any]:
        if self.fetch_remote:
            for path in (
                "/public/summary",
                "/public/daily?days=14",
                "/public/leaderboard?limit=20",
                "/version",
                "/health/live",
                "/health/ready",
            ):
                data = _safe_fetch_json(path)
                if data is not None:
                    self.remote[path] = data
        return {
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "local": self._local_summary(),
            "public": self.remote.get("/public/summary") if self.fetch_remote else None,
            "public_daily": self.remote.get("/public/daily?days=14") if self.fetch_remote else None,
            "public_leaderboard": self.remote.get("/public/leaderboard?limit=20") if self.fetch_remote else None,
            "public_version": (self.remote.get("/version") or {}).get("version") if self.fetch_remote else None,
            "fetch_remote": self.fetch_remote,
            "offline_mode": not self.fetch_remote,
        }

    def _local_summary(self) -> Dict[str, Any]:
        rows = self.local_rows
        started = finished = failed = pending = quarantined = 0
        per_tc_started: Dict[str, int] = {}
        per_tc_finished: Dict[str, int] = {}
        per_tc_durations: Dict[str, List[float]] = {}
        per_tc_success: Dict[str, int] = {}
        encoder_counts: Counter = Counter()
        failure_codes: Counter = Counter()
        per_day: Counter = Counter()
        per_lens: Counter = Counter()
        for row in rows:
            status = row["status"]
            if status == "pending":
                pending += 1
            elif status == "failed":
                failed += 1
            elif status == "quarantined":
                quarantined += 1
            p = row["payload"]
            occ = (p.get("occurred_at") or "").split("T")[0]
            if occ:
                per_day[occ] += 1
            et = p.get("event_type", "")
            if et == "RUN_STARTED":
                started += 1
                tc = p.get("label") or "?"
                per_tc_started[tc] = per_tc_started.get(tc, 0) + 1
                enc = p.get("encoder") or p.get("profile", {}).get("encoder")
                if enc:
                    encoder_counts[enc] += 1
                lens = p.get("profile", {}).get("lens")
                if lens:
                    per_lens[lens] += 1
            elif et == "RUN_FINISHED":
                finished += 1
                tc = p.get("label") or "?"
                per_tc_finished[tc] = per_tc_finished.get(tc, 0) + 1
                if p.get("status") == "SUCCEEDED":
                    per_tc_success[tc] = per_tc_success.get(tc, 0) + 1
                else:
                    fc = p.get("failure", {}).get("code") or p.get("status") or "?"
                    failure_codes[fc] += 1
                dur_ms = p.get("duration_ms") or 0
                if dur_ms > 0:
                    per_tc_durations.setdefault(tc, []).append(dur_ms / 1000.0)
        return {
            "total_events": len(rows),
            "started": started,
            "finished": finished,
            "failed": failed,
            "pending": pending,
            "quarantined": quarantined,
            "per_tc_started": per_tc_started,
            "per_tc_finished": per_tc_finished,
            "per_tc_success": per_tc_success,
            "per_tc_p50": {tc: round(median(d), 2) for tc, d in per_tc_durations.items() if d},
            "per_tc_p95": {tc: round(_p95(d), 2) for tc, d in per_tc_durations.items() if d},
            "encoder_counts": dict(encoder_counts),
            "per_lens": dict(per_lens),
            "failure_codes": dict(failure_codes),
            "per_day": dict(per_day),
        }


def _seed_sample_events(db_path: Path, *, count: int = 12) -> int:
    """Insert sample events so the dashboard renders something on first run."""
    if not db_path.parent.is_dir():
        db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch(exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                event_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0,
                payload_bytes INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS quarantine (
                event_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                quarantined_at REAL NOT NULL
            );
            """
        )
        now = time.time()
        tcs = ["TC01", "TC02", "TC03", "TC04", "TC05", "TC06"]
        encoders = ["h264_nvenc", "hevc_nvenc", "libx264", "av1_nvenc"]
        lenses = ["lens35mm", "lens50mm", "lens16mm", "lens85mm"]
        failures = [
            ("h264_nvenc_init", 2),
            ("chromakey_invalid_key", 1),
            ("output_write_denied", 1),
            ("ffprobe_missing_stream", 1),
        ]
        for i in range(count):
            tc = tcs[i % len(tcs)]
            enc = encoders[i % len(encoders)]
            lens = lenses[i % len(lenses)]
            phase = "RUN_STARTED" if i % 3 != 1 else "RUN_FINISHED"
            payload = {
                "event_type": phase,
                "client_run_id": f"seeder-{i:04d}",
                "occurred_at": datetime.utcfromtimestamp(now - (count - i) * 90).isoformat() + "Z",
                "label": tc,
                "encoder": enc,
                "schema_version": 6,
                "profile": {"encoder": enc, "lens": lens},
            }
            if phase == "RUN_FINISHED":
                ok = (i % 4) != 0
                payload["status"] = "SUCCEEDED" if ok else "FAILED"
                if not ok:
                    payload["failure"] = {"code": failures[i % len(failures)][0]}
                payload["duration_ms"] = 8_000 + (i * 173) % 12_000
            event_id = f"seeder-{i:04d}-{phase[-7:].lower()}"
            conn.execute(
                "INSERT OR IGNORE INTO outbox (event_id, payload_json, attempts, next_attempt_at, created_at, payload_bytes) "
                "VALUES (?, ?, 0, 0, ?, ?)",
                (event_id, json.dumps(payload), now - (count - i) * 90, len(json.dumps(payload))),
            )
        conn.commit()
        return count


class PWAHandler(http.server.BaseHTTPRequestHandler):
    server_version = "SJ88PreviewPWA/0.1"
    data: Dict[str, Any] = {}

    PWA_MANIFEST = json.dumps({
        "name": "SJ88 Local Stats",
        "short_name": "SJ88 Stats",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0c1218",
        "theme_color": "#7fd962",
        "icons": [
            {
                "src": "/icon-192.svg",
                "sizes": "192x192",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }, indent=2)

    PWA_ICON = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
        '<rect width="192" height="192" rx="40" fill="#0c1218"/>'
        '<text x="96" y="120" text-anchor="middle" font-family="sans-serif" '
        'font-size="80" font-weight="700" fill="#7fd962">SJ</text>'
        '</svg>'
    )

    PWA_CSS = """
:root { --bg:#0c1218; --panel:#16212b; --border:#233140; --text:#e6edf3;
  --muted:#7f8a99; --good:#3ddc97; --warn:#f7c548; --bad:#ff6b6b; --link:#3aa7ff; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, "Segoe UI", Arial, sans-serif; }
header { background: linear-gradient(180deg, #16212b, transparent); padding: 16px 24px;
  border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 10; }
header h1 { margin: 0; font-size: 18px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; padding: 18px 24px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 12px; }
.card .lbl { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
.card .val { font-size: 22px; font-weight: 700; }
.card .val.good { color: var(--good); }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--good); margin-right: 6px; vertical-align: middle; animation: pulse 1.5s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
table { border-collapse: collapse; width: calc(100% - 48px); margin: 12px 24px; font-size: 12px; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--border); text-align: left; }
th { color: var(--muted); text-transform: uppercase; font-size: 10px; }
.bar { height: 6px; background: #1a232e; border-radius: 3px; }
.bar > i { display: block; height: 100%; background: linear-gradient(90deg, var(--good), var(--link)); border-radius: 3px; }
.per-lens { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; padding: 0 24px; }
.per-lens .lens { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
.per-lens .lens .nm { color: var(--muted); font-size: 11px; }
.per-lens .lens .ct { font-size: 18px; font-weight: 700; color: var(--good); }
"""

    PWA_JS = """
async function fetchJSON(path, init) {
  const r = await fetch(path, init || {});
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function tick() {
  try {
    const data = await fetchJSON("/api/stats.json");
    render(data);
  } catch (e) {
    console.warn("tick failed", e);
  }
}

function render(d) {
  const root = document.getElementById("root");
  if (!root) return;
  const local = d.local || {};
  const fmt = (n) => (n == null ? "0" : n.toLocaleString());
  root.innerHTML = `
    <header>
      <h1><span class="dot"></span>SJ88 Live (SSE) ${d.offline_mode ? "[offline]" : ""}</h1>
    </header>
    <div class="cards">
      <div class="card"><div class="lbl">Total events</div><div class="val">${fmt(local.total_events)}</div></div>
      <div class="card"><div class="lbl">RUN_STARTED</div><div class="val good">${fmt(local.started)}</div></div>
      <div class="card"><div class="lbl">RUN_FINISHED</div><div class="val">${fmt(local.finished)}</div></div>
      <div class="card"><div class="lbl">failed</div><div class="val bad">${fmt(local.failed)}</div></div>
      <div class="card"><div class="lbl">pending</div><div class="val" style="color:var(--warn)">${fmt(local.pending)}</div></div>
    </div>
    <table>
      <thead><tr><th>TC</th><th>started</th><th>finished</th><th>success</th><th>p50</th><th>p95</th></tr></thead>
      <tbody>
        ${Object.entries(local.per_tc_started || {}).map(([tc, s]) => {
          const f = (local.per_tc_finished || {})[tc] || 0;
          const ok = (local.per_tc_success || {})[tc] || 0;
          const p50 = (local.per_tc_p50 || {})[tc] || 0;
          const p95 = (local.per_tc_p95 || {})[tc] || 0;
          const pct = f ? (ok * 100 / f) : 0;
          const cls = pct >= 90 ? "good" : pct >= 60 ? "" : "bad";
          return "<tr><td><b>" + tc + "</b></td><td>" + s + "</td><td>" + f + "</td><td class='" + cls + "'>" + ok + "</td><td>" + p50.toFixed(2) + "s</td><td>" + p95.toFixed(2) + "s</td></tr>";
        }).join("")}
      </tbody>
    </table>
    <div class="per-lens">
      ${Object.entries(local.per_lens || {}).slice(0, 12).map(([k, n]) =>
        "<div class='lens'><div class='nm'>" + k + "</div><div class='ct'>" + n + "</div></div>"
      ).join("")}
    </div>
  `;
}

setInterval(tick, 2000);
tick();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/service-worker.js").catch(() => {});
}
"""

    PWA_SW_JS = """
const CACHE = "sj88-stats-v1";
const SHELL = ["/", "/style.css", "/live.js", "/manifest.webmanifest", "/icon-192.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
  )));
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(caches.match(e.request).then((cached) => {
    if (cached) return cached;
    return fetch(e.request).then((res) => {
      if (res.ok && e.request.url.startsWith(self.location.origin)) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return res;
    }).catch(() => caches.match("/"));
  }));
});
"""

    SHELL_HTML = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<title>SJ88 Live</title>'
        '<link rel="manifest" href="/manifest.webmanifest">'
        '<link rel="stylesheet" href="/style.css">'
        '<script src="/live.js" defer></script>'
        '</head><body><main id="root"></main>'
        '</body></html>'
    )

    def do_GET(self) -> None:  # noqa: N802
        path = self.path
        if path in ("/", "/index.html", "/dashboard", "/stats"):
            body = self.SHELL_HTML.encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
            return
        if path == "/style.css":
            self._send(200, "text/css; charset=utf-8", self.PWA_CSS.encode("utf-8"))
            return
        if path == "/live.js":
            self._send(200, "application/javascript; charset=utf-8", self.PWA_JS.encode("utf-8"))
            return
        if path == "/service-worker.js":
            self._send(200, "application/javascript; charset=utf-8", self.PWA_SW_JS.encode("utf-8"))
            return
        if path == "/manifest.webmanifest":
            self._send(200, "application/manifest+json", self.PWA_MANIFEST.encode("utf-8"))
            return
        if path == "/icon-192.svg":
            self._send(200, "image/svg+xml", self.PWA_ICON.encode("utf-8"))
            return
        if path == "/api/seeder/preview":
            db = DEFAULT_OUTBOX
            _seed_sample_events(db)
            body = json.dumps({"status": "seeded", "db": str(db), "count": 12}).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if path == "/api/stream":
            self._stream()
            return
        if path == "/api/stats.json":
            body = json.dumps(self.data, ensure_ascii=False, indent=2).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if path == "/healthz":
            self._send(200, "application/json", b'{"status":"ok"}')
            return
        self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        """Server-Sent Events: push the latest dashboard data every 2s."""
        self._send_sse_headers()
        for _ in range(60):  # ~2 min cap per connection
            try:
                payload = json.dumps(self.data, ensure_ascii=False).encode("utf-8")
                self.wfile.write(b"event: stats\ndata: ")
                self.wfile.write(payload)
                self.wfile.write(b"\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            time.sleep(2.0)

    def _send_sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[preview-pwa] {fmt % args}\n")


def serve(port: int, bind: str, fetch_remote: bool, outbox: Path, seed: bool) -> None:
    if seed:
        try:
            n = _seed_sample_events(outbox)
            print(f"[preview-pwa] seeded {n} sample events into {outbox}")
        except Exception as exc:
            print(f"[preview-pwa] seeder failed: {exc}")
    builder = DashboardBuilder(outbox=outbox, fetch_remote=fetch_remote)
    PWAHandler.data = builder.build()
    class ReuseAddressServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
    with ReuseAddressServer((bind, port), PWAHandler) as httpd:
        print(f"[preview-pwa] http://{bind}:{port}/  (Ctrl+C to quit)")
        print(f"[preview-pwa] SSE endpoint: /api/stream")
        print(f"[preview-pwa] PWA: /manifest.webmanifest, /service-worker.js")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("[preview-pwa] shutting down")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--outbox", default=str(DEFAULT_OUTBOX))
    p.add_argument("--no-remote", action="store_true")
    p.add_argument("--seed", action="store_true",
                   help="inject 12 sample events so the dashboard has data")
    args = p.parse_args(argv)
    serve(
        port=args.port,
        bind=args.bind,
        fetch_remote=not args.no_remote,
        outbox=Path(os.path.expanduser(args.outbox)),
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
