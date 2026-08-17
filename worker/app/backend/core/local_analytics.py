"""Read-only local analytics from the GreenStats outbox.

Parses ``~/.green_pc/usage_stats_outbox.sqlite3`` and produces a small
dashboard that answers:

  - how many renders have we attempted in total / by TC / by day?
  - what is the success rate per TC?
  - what is the p50 / p95 render duration per TC?
  - what is the failure breakdown?
  - how many events are still pending / failed / quarantined?
  - which encoding was used most / which lens?

Useful for: shipping with desktop, debug, "what does my install do?"
and as a minimal offline companion to GreenStats.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional


@dataclass
class LocalRow:
    id: int
    event_type: str
    event_id: str
    client_run_id: str
    occurred_at: str
    payload: Dict[str, Any]
    delivery_status: str
    delivery_attempts: int


def _read_outbox(db_path: Path) -> List[LocalRow]:
    if not db_path.is_file():
        return []
    rows: List[LocalRow] = []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # outbox table schema: event_id, payload_json, attempts, next_attempt_at,
        # created_at, payload_bytes.  status + delivery_status are inferred from
        # attempts + quarantine-table presence.
        quarantine_events = {
            row["event_id"]
            for row in conn.execute("SELECT event_id FROM quarantine")
        }
        for row in conn.execute(
            "SELECT event_id, payload_json, attempts, next_attempt_at, created_at, "
            "payload_bytes FROM outbox ORDER BY created_at DESC"
        ):
            try:
                payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            except Exception:
                payload = {}
            attempts = int(row["attempts"] or 0)
            event_id = row["event_id"]
            if event_id in quarantine_events:
                status = "quarantined"
            elif attempts > 0:
                status = "failed"
            else:
                status = "pending"
            rows.append(LocalRow(
                id=0,
                event_type=payload.get("event_type", "UNKNOWN"),
                event_id=event_id,
                client_run_id=payload.get("client_run_id", ""),
                occurred_at=payload.get("occurred_at", ""),
                payload=payload,
                delivery_status=status,
                delivery_attempts=attempts,
            ))
    return rows


def _p95(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, int(round(0.95 * (len(s) - 1))))
    return s[k]


@dataclass
class LocalDashboard:
    """In-memory snapshot of the local outbox.

    All count fields default to zero so ``build_dashboard`` can populate
    them incrementally; ``per_*`` dicts start empty. ``generated_at`` is the
    UTC stamp of the moment this snapshot was assembled (also useful for the
    Tk panel subtitle line).
    """

    total_events: int = 0
    started: int = 0
    finished: int = 0
    failed: int = 0
    pending: int = 0
    quarantined: int = 0
    per_tc_started: Dict[str, int] = field(default_factory=dict)
    per_tc_finished: Dict[str, int] = field(default_factory=dict)
    per_tc_success: Dict[str, int] = field(default_factory=dict)
    per_tc_p50: Dict[str, float] = field(default_factory=dict)
    per_tc_p95: Dict[str, float] = field(default_factory=dict)
    encoder_counts: Dict[str, int] = field(default_factory=dict)
    lens_counts: Dict[str, int] = field(default_factory=dict)
    failure_codes: Counter = field(default_factory=Counter)
    per_day: Dict[str, int] = field(default_factory=dict)
    generated_at: str = ""


def build_dashboard(db_path: Path) -> LocalDashboard:
    rows = _read_outbox(db_path)
    started = finished = failed = pending = quarantined = 0
    per_tc_started: Dict[str, int] = {}
    per_tc_finished: Dict[str, int] = {}
    per_tc_success: Dict[str, int] = {}
    per_tc_durations: Dict[str, List[float]] = {}
    encoder_counts: Counter = Counter()
    lens_counts: Counter = Counter()
    failure_codes: Counter = Counter()
    per_day: Counter = Counter()

    for row in rows:
        if row.delivery_status == "pending":
            pending += 1
        if row.delivery_status == "quarantined":
            quarantined += 1
        if row.event_type == "RUN_STARTED":
            started += 1
            tc = row.payload.get("label") or "?"
            per_tc_started[tc] = per_tc_started.get(tc, 0) + 1
            enc = row.payload.get("encoder")
            if enc:
                encoder_counts[enc] += 1
            # Lens tag is carried under profile.lens; mirror preview_web_server.
            lens = row.payload.get("profile", {}).get("lens") or row.payload.get("lens")
            if lens:
                lens_counts[lens] += 1
            occ = row.occurred_at.split("T")[0] if row.occurred_at else "?"
            per_day[occ] += 1
        elif row.event_type == "RUN_FINISHED":
            finished += 1
            tc = row.payload.get("label") or "?"
            status = row.payload.get("status") or "?"
            per_tc_finished[tc] = per_tc_finished.get(tc, 0) + 1
            if status == "SUCCEEDED":
                per_tc_success[tc] = per_tc_success.get(tc, 0) + 1
            else:
                failed += 1
                code = row.payload.get("failure", {}).get("code", status)
                failure_codes[code] += 1
            dur_ms = row.payload.get("duration_ms") or 0
            if dur_ms > 0:
                per_tc_durations.setdefault(tc, []).append(dur_ms / 1000.0)
            occ = row.occurred_at.split("T")[0] if row.occurred_at else "?"
            per_day[occ] += 1

    return LocalDashboard(
        total_events=len(rows),
        started=started,
        finished=finished,
        failed=failed,
        pending=pending,
        quarantined=quarantined,
        per_tc_started=per_tc_started,
        per_tc_finished=per_tc_finished,
        per_tc_success=per_tc_success,
        per_tc_p50={tc: median(d) for tc, d in per_tc_durations.items() if d},
        per_tc_p95={tc: _p95(d) for tc, d in per_tc_durations.items() if d},
        encoder_counts=dict(encoder_counts),
        lens_counts=dict(lens_counts),
        failure_codes=failure_codes,
        per_day=dict(per_day),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
    )


def _fmt_row(label: str, value: str, width: int = 40) -> str:
    return f"  {label.ljust(width - len(value))}{value}"


def render_text(dash: LocalDashboard) -> str:
    out = []
    out.append("=" * 60)
    out.append("  SJ88 Green Screen -- Local Stats (offline)")
    out.append("=" * 60)
    out.append(_fmt_row("Total events:", str(dash.total_events)))
    out.append(_fmt_row("RUN_STARTED:", str(dash.started)))
    out.append(_fmt_row("RUN_FINISHED:", str(dash.finished)))
    out.append(_fmt_row("  failed:", str(dash.failed)))
    out.append(_fmt_row("Pending dispatch:", str(dash.pending)))
    out.append(_fmt_row("Quarantined:", str(dash.quarantined)))
    out.append("")
    out.append("Per-TC success rate")
    out.append("-" * 60)
    if not dash.per_tc_started:
        out.append("  (no events yet)")
    else:
        all_tcs = sorted(set(dash.per_tc_started) | set(dash.per_tc_finished))
        for tc in all_tcs:
            s = dash.per_tc_started.get(tc, 0)
            f = dash.per_tc_finished.get(tc, 0)
            ok = dash.per_tc_success.get(tc, 0)
            pct = (ok / f * 100.0) if f else 0.0
            p50 = dash.per_tc_p50.get(tc, 0.0)
            p95 = dash.per_tc_p95.get(tc, 0.0)
            out.append(
                f"  {tc:<6} started={s:>3} finished={f:>3} success={ok:>3} "
                f"({pct:5.1f}%)  p50={p50:5.2f}s  p95={p95:5.2f}s"
            )
    out.append("")
    out.append("Encoder distribution")
    out.append("-" * 60)
    if not dash.encoder_counts:
        out.append("  (no encoders recorded yet)")
    else:
        for enc, n in sorted(dash.encoder_counts.items(), key=lambda kv: -kv[1]):
            out.append(f"  {enc:<28} {n:>5}")
    if dash.failure_codes:
        out.append("")
        out.append("Failure breakdown")
        out.append("-" * 60)
        for code, n in dash.failure_codes.most_common(5):
            out.append(f"  {code:<32} {n:>3}")
    out.append("")
    out.append("Daily events (last 30 days)")
    out.append("-" * 60)
    days = sorted(dash.per_day.items())[-30:]
    if not days:
        out.append("  (no events yet)")
    else:
        max_n = max(n for _, n in days) if days else 1
        for d, n in days:
            bar = "#" * int(round(n * 30 / max_n))
            out.append(f"  {d}  {n:>4}  {bar}")
    out.append("=" * 60)
    return "\n".join(out)


def render_html(dash: LocalDashboard) -> str:
    """PWA-friendly single-file HTML dashboard."""
    rows = []
    rows.append("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    rows.append("<title>SJ88 Local Stats</title>")
    rows.append("<style>")
    rows.append("body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;background:#0f1620;color:#e8e8f0}")
    rows.append("main{padding:24px;max-width:980px;margin:0 auto}")
    rows.append("h1{margin:0 0 8px;font-size:22px;color:#7fd962}")
    rows.append("h2{margin:24px 0 8px;font-size:16px;color:#9aa8b6;font-weight:600}")
    rows.append(".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}")
    rows.append(".card{background:#1a2530;border:1px solid #293847;border-radius:6px;padding:12px}")
    rows.append(".v{font-size:22px;color:#7fd962;font-weight:700}")
    rows.append(".l{font-size:11px;color:#9aa8b6;text-transform:uppercase}")
    rows.append("table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}")
    rows.append("td,th{padding:6px 10px;border-bottom:1px solid #293847;text-align:left}")
    rows.append("th{background:#0e1721;color:#9aa8b6;text-transform:uppercase;font-size:11px}")
    rows.append(".ok{color:#7fd962}.warn{color:#f0b070}.bad{color:#ef5b5b}")
    rows.append(".bar{height:14px;background:#0e1721;border-radius:3px;position:relative}")
    rows.append(".bar > i{display:block;height:100%;background:linear-gradient(90deg,#7fd962,#3da4ff);border-radius:3px}")
    rows.append(".grid-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-top:6px}")
    rows.append("</style></head><body><main>")
    rows.append("<h1>SJ88 Green Screen - Local Stats <span style='font-size:11px;color:#9aa8b6'>(offline)</span></h1>")
    rows.append("<div class='l'>Read from <code>~/.green_pc/usage_stats_outbox.sqlite3</code></div>")

    def card(label: str, value: Any, klass: str = "") -> str:
        return (
            f"<div class='card'>"
            f"<div class='l'>{label}</div>"
            f"<div class='v {klass}'>{value}</div>"
            f"</div>"
        )

    rows.append("<h2>Pipeline totals</h2><div class='grid'>")
    rows.append(card("Total events", dash.total_events))
    rows.append(card("RUN_STARTED", dash.started))
    rows.append(card("RUN_FINISHED", dash.finished))
    rows.append(card("failed", dash.failed, "bad"))
    rows.append(card("pending", dash.pending, "warn"))
    rows.append(card("quarantined", dash.quarantined, "warn"))
    rows.append("</div>")

    rows.append("<h2>Per-TC</h2>")
    if dash.per_tc_started:
        rows.append("<table><thead><tr><th>TC</th><th>started</th><th>finished</th>")
        rows.append("<th>success</th><th>rate</th><th>p50</th><th>p95</th></tr></thead><tbody>")
        all_tcs = sorted(set(dash.per_tc_started) | set(dash.per_tc_finished))
        for tc in all_tcs:
            s = dash.per_tc_started.get(tc, 0)
            f = dash.per_tc_finished.get(tc, 0)
            ok = dash.per_tc_success.get(tc, 0)
            pct = (ok / f * 100.0) if f else 0.0
            p50 = dash.per_tc_p50.get(tc, 0.0)
            p95 = dash.per_tc_p95.get(tc, 0.0)
            klass = "ok" if pct >= 90 else ("warn" if pct >= 60 else "bad")
            rows.append(
                f"<tr><td>{tc}</td><td>{s}</td><td>{f}</td>"
                f"<td class='{klass}'>{ok}</td>"
                f"<td class='{klass}'>{pct:.1f}%</td>"
                f"<td>{p50:.2f}s</td><td>{p95:.2f}s</td></tr>"
            )
        rows.append("</tbody></table>")
    else:
        rows.append("<div class='l'>No events yet</div>")

    rows.append("<h2>Encoder distribution</h2>")
    if dash.encoder_counts:
        rows.append("<div class='grid-cards'>")
        for enc, n in sorted(dash.encoder_counts.items(), key=lambda kv: -kv[1])[:12]:
            rows.append(card(enc, n))
        rows.append("</div>")
    else:
        rows.append("<div class='l'>No encoders recorded yet</div>")

    if dash.failure_codes:
        rows.append("<h2>Top failures</h2><table><thead><tr><th>code</th><th>count</th></tr></thead><tbody>")
        for code, n in dash.failure_codes.most_common(10):
            rows.append(f"<tr><td>{code}</td><td class='bad'>{n}</td></tr>")
        rows.append("</tbody></table>")

    rows.append("<h2>Daily events (last 30 days)</h2>")
    days = sorted(dash.per_day.items())[-30:]
    if days:
        max_n = max(n for _, n in days) if days else 1
        rows.append("<table><thead><tr><th>date</th><th>count</th><th></th></tr></thead><tbody>")
        for d, n in days:
            pct = (n * 100 // max_n) if max_n else 0
            rows.append(
                f"<tr><td>{d}</td><td>{n}</td>"
                f"<td><div class='bar'><i style='width:{pct}%'></i></div></td></tr>"
            )
        rows.append("</tbody></table>")
    else:
        rows.append("<div class='l'>No events yet</div>")

    rows.append("</main></body></html>")
    return "\n".join(rows)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="~/.green_pc/usage_stats_outbox.sqlite3",
                   help="path to outbox sqlite (default: ~/.green_pc/...)")
    p.add_argument("--format", choices=["text", "html", "json"], default="text")
    p.add_argument("--out", default=None, help="write to file instead of stdout")
    args = p.parse_args(argv)

    db_path = Path(os.path.expanduser(args.db))
    dash = build_dashboard(db_path)
    if args.format == "json":
        import dataclasses
        rendered = json.dumps(dataclasses.asdict(dash), ensure_ascii=False, indent=2)
    elif args.format == "html":
        rendered = render_html(dash)
    else:
        rendered = render_text(dash)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
