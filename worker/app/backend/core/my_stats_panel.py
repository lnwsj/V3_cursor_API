"""Tiny Tkinter panel that renders local stats from the outbox.

Can be opened from the desktop app via the main window menu or as a
standalone script:

  py -m core.my_stats_panel

Renders the same data shape as ``preview_web_server`` so the local view
matches the public dashboard. Uses ttk only -- no extra deps.
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict


# Reuse the local analytics logic
from core.local_analytics import build_dashboard
from core.usage_stats import bootstrap_temp_identity, load_config


DEFAULT_OUTBOX = Path.home() / ".green_pc" / "usage_stats_outbox.sqlite3"


class MyStatsPanel:
    def __init__(self, *, outbox: Path = DEFAULT_OUTBOX, master: tk.Tk | None = None):
        self.outbox = outbox
        self.master = master or tk.Tk()
        self.master.title("SJ88 · My Local Stats")
        self.master.geometry("780x600")
        self._build_styles()
        self._build_widgets()
        self._populate()

    def _build_styles(self) -> None:
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("Heading.TLabel", font=("Segoe UI", 14, "bold"))
        s.configure("Sub.TLabel", foreground="#666")
        s.configure("Metric.TLabel", font=("Segoe UI", 22, "bold"))
        s.configure("Metric.TLabel.success", foreground="#2fa572")
        s.configure("Metric.TLabel.fail", foreground="#c43a3a")
        s.configure("Metric.TLabel.warn", foreground="#c08838")
        s.configure("Section.TLabel", font=("Segoe UI", 11, "bold"), foreground="#333")

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.master, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="SJ88 Green Screen · My Local Stats", style="Heading.TLabel").pack(anchor="w")
        self.subtitle = ttk.Label(outer, text="", style="Sub.TLabel")
        self.subtitle.pack(anchor="w", pady=(0, 12))

        # Identity strip
        idstrip = ttk.Frame(outer)
        idstrip.pack(fill="x", pady=(0, 12))
        ttk.Label(idstrip, text="Identity:", style="Section.TLabel").pack(side="left")
        self.identity_label = ttk.Label(idstrip, text="")
        self.identity_label.pack(side="left", padx=(8, 0))
        ttk.Button(idstrip, text="Bootstrap temp nickname",
                   command=self._bootstrap_identity).pack(side="right", padx=4)
        ttk.Button(idstrip, text="Refresh",
                   command=self._populate).pack(side="right", padx=4)

        # Metric grid
        grid = ttk.Frame(outer)
        grid.pack(fill="x", pady=(0, 16))
        self._metric_vars: Dict[str, tk.StringVar] = {}
        for i, key in enumerate(("total_events", "started", "finished", "failed",
                                  "pending", "quarantined")):
            cell = ttk.Frame(grid, padding=12, relief="groove", borderwidth=1)
            cell.grid(row=i // 3, column=i % 3, sticky="nsew", padx=6, pady=6)
            grid.grid_columnconfigure(i % 3, weight=1)
            ttk.Label(cell, text=key.replace("_", " ").upper(),
                      style="Sub.TLabel").pack(anchor="w")
            var = tk.StringVar(value="-")
            ttk.Label(cell, textvariable=var, style="Metric.TLabel").pack(anchor="w")
            self._metric_vars[key] = var

        # Per-TC table
        ttk.Label(outer, text="Per-TC", style="Section.TLabel").pack(anchor="w")
        ttk.Frame(outer, height=4).pack()
        cols = ("tc", "started", "finished", "success", "p50_s", "p95_s")
        self.tree = ttk.Treeview(outer, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (90, 80, 80, 80, 80, 80)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", pady=(0, 8))

        # Per-encoder
        ttk.Label(outer, text="Encoder distribution", style="Section.TLabel").pack(anchor="w")
        self.encoder_box = ttk.Frame(outer, relief="groove", borderwidth=1, padding=8)
        self.encoder_box.pack(fill="x", pady=(0, 8))

        # Per-lens
        ttk.Label(outer, text="Lens distribution", style="Section.TLabel").pack(anchor="w")
        self.lens_box = ttk.Frame(outer, relief="groove", borderwidth=1, padding=8)
        self.lens_box.pack(fill="x")

        # Footer
        foot = ttk.Frame(outer)
        foot.pack(fill="x", pady=(12, 0))
        ttk.Button(foot, text="Close", command=self.master.destroy).pack(side="right")

    def _populate(self) -> None:
        try:
            cfg = load_config()
            ident = cfg.identity if cfg and cfg.identity else None
        except Exception:
            ident = None
        if ident and (ident.identity_type or ident.identity_value):
            self.identity_label.config(
                text=f"{ident.identity_type}: {ident.identity_value}",
                foreground="#2fa572")
        else:
            self.identity_label.config(text="(no identity set)", foreground="#c08838")

        try:
            dash = build_dashboard(self.outbox)
        except Exception as exc:
            self.subtitle.config(text=f"error: {exc}")
            return

        self.subtitle.config(
            text=f"outbox={self.outbox}  "
                 f"generated_at={dash.generated_at or '-'}"
        )
        self._metric_vars["total_events"].set(str(dash.total_events))
        self._metric_vars["started"].set(str(dash.started))
        self._metric_vars["finished"].set(str(dash.finished))
        self._metric_vars["failed"].set(str(dash.failed))
        self._metric_vars["pending"].set(str(dash.pending))
        self._metric_vars["quarantined"].set(str(dash.quarantined))

        # Update tree
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        all_tcs = sorted(set(dash.per_tc_started) | set(dash.per_tc_finished))
        for tc in all_tcs:
            s = dash.per_tc_started.get(tc, 0)
            f = dash.per_tc_finished.get(tc, 0)
            ok = dash.per_tc_success.get(tc, 0)
            p50 = dash.per_tc_p50.get(tc, 0.0)
            p95 = dash.per_tc_p95.get(tc, 0.0)
            self.tree.insert("", "end", values=(
                tc, s, f, ok, f"{p50:.2f}", f"{p95:.2f}"))

        # Update encoders
        for w in self.encoder_box.winfo_children():
            w.destroy()
        encs = dash.encoder_counts
        total = sum(encs.values()) or 1
        for enc, n in sorted(encs.items(), key=lambda kv: -kv[1]):
            ttk.Label(self.encoder_box,
                      text=f"  {enc:<24} {n:>5}  {'#' * int(round(n * 20 / max(total, 1)))}").pack(anchor="w")

        # Update lenses
        for w in self.lens_box.winfo_children():
            w.destroy()
        lens = dash.lens_counts
        lens_total = sum(lens.values()) or 1
        if not lens:
            ttk.Label(self.lens_box, text="  (no lens data yet)",
                      style="Sub.TLabel").pack(anchor="w")
        else:
            for lname, n in sorted(lens.items(), key=lambda kv: -kv[1]):
                ttk.Label(self.lens_box,
                          text=f"  {lname:<18} {n:>4}  {'#' * int(round(n * 18 / max(lens_total, 1)))}").pack(anchor="w")

    def _bootstrap_identity(self) -> None:
        try:
            identity = bootstrap_temp_identity()
            message = f"Bootstrapped {identity.identity_type}: {identity.identity_value}"
        except Exception as exc:
            message = f"Bootstrap failed: {exc}"
        self._show_message("Bootstrap", message)
        self._populate()

    def _show_message(self, title: str, message: str) -> None:
        top = tk.Toplevel(self.master)
        top.title(title)
        ttk.Label(top, text=message, padding=14).pack(fill="x")

    def run(self) -> None:
        self.master.mainloop()


def main(argv=None) -> int:
    master = None
    if argv and argv[0] in ("--child", "--attach"):
        master = tk.Tk()
        master.withdraw()
    panel = MyStatsPanel(master=master)
    panel.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
