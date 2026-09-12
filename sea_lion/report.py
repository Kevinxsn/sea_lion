"""Daily JSON + HTML report and performance metrics."""
from __future__ import annotations

import html
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .store import Store


def perf_metrics(equity: List[float], bench: Optional[List[float]] = None, rf: float = 0.0) -> Dict[str, Any]:
    """Net return, vol, max drawdown, Sharpe, hit rate, and benchmark excess from a daily equity path."""
    out: Dict[str, Any] = {"n_days": max(len(equity) - 1, 0)}
    if len(equity) < 2:
        return out
    e = np.asarray(equity, dtype=float)
    r = np.diff(e) / e[:-1]
    out["total_return"] = float(e[-1] / e[0] - 1)
    out["ann_return"] = float((e[-1] / e[0]) ** (252 / max(len(r), 1)) - 1) if len(r) >= 20 else None
    out["ann_vol"] = float(r.std(ddof=1) * math.sqrt(252)) if len(r) > 2 else None
    out["sharpe"] = float((r.mean() - rf / 252) / r.std(ddof=1) * math.sqrt(252)) if len(r) > 2 and r.std(ddof=1) > 0 else None
    peak = np.maximum.accumulate(e)
    out["max_drawdown"] = float(((e - peak) / peak).min())
    out["hit_rate"] = float((r > 0).mean())
    if bench and len(bench) == len(equity):
        b = np.asarray(bench, dtype=float)
        out["benchmark_return"] = float(b[-1] / b[0] - 1)
        out["excess_return"] = out["total_return"] - out["benchmark_return"]
    out["small_sample_warning"] = len(r) < 60
    return out


def build_report(store: Store, run_id: str, mode: str) -> Dict[str, Any]:
    run = dict(store.run(run_id) or {})
    stages = {r["stage"]: {"status": r["status"], "error": r["error"]} for r in
              store.q("SELECT stage,status,error FROM run_stages WHERE run_id=?", (run_id,))}
    art = lambda s: store.stage_artifact(run_id, s) or {}  # noqa: E731
    decisions = [dict(r) for r in store.q("SELECT * FROM decisions WHERE run_id=? ORDER BY rank NULLS LAST, ensemble_score DESC", (run_id,))]
    orders = store.orders_for_run(run_id)
    calls = [dict(r) for r in store.q("SELECT tier,provider,model,cache_hit,input_tokens,output_tokens,latency_ms,cost_usd,valid,error FROM model_calls WHERE run_id=?", (run_id,))]
    hist = store.equity_history(mode)
    eq = [h["equity"] for h in hist]
    bench = [h["benchmark_close"] for h in hist] if all(h.get("benchmark_close") for h in hist) else None
    metrics = perf_metrics(eq, bench)
    as_of = run.get("as_of", "")
    month = as_of[:7] + "-01" if as_of else ""
    costs = {"ai_today": store.cost_sum("ai.", as_of, as_of), "ai_month": store.cost_sum("ai.", month, as_of),
             "all_month": store.cost_sum("", month, as_of)}
    # ---- V2 sections
    arms = {r["arm"]: dict(r) for r in store.q("SELECT * FROM portfolio_arms WHERE run_id=?", (run_id,))}
    for a in arms.values():
        for k in ("target_weights_json", "candidates_json", "approved_json", "intents_json", "sim_positions_json", "assumptions_json"):
            a[k[:-5]] = json.loads(a.pop(k) or "null")
    div = {r["arm"]: dict(r) for r in store.q("SELECT * FROM decision_divergence WHERE run_id=?", (run_id,))}
    for d in div.values():
        for k in ("rank_changes_json", "membership_added_json", "membership_removed_json", "return_diff_json"):
            d[k[:-5]] = json.loads(d.pop(k) or "null")
    fcs = [dict(r) for r in store.q("SELECT symbol, arm, abstain, evidence_quality, model_disagreement, overlay_score, calibrated_json, "
                                    "evidence_ids_json, risk_flags_json FROM forecasts WHERE run_id=? ORDER BY arm, symbol", (run_id,))]
    v2_art = store.stage_artifact(run_id, "v2_research") or {}
    packets = v2_art.get("forecasts") or {}
    for f in fcs:
        f["calibrated"] = json.loads(f.pop("calibrated_json") or "{}")
        f["evidence_ids"] = json.loads(f.pop("evidence_ids_json") or "[]")
        f["risk_flags"] = json.loads(f.pop("risk_flags_json") or "[]")
        f["reason"] = (packets.get(f["symbol"]) or {}).get("abstain_reason", "")[:120]
    matured = [dict(r) for r in store.q("SELECT o.*, f.symbol, f.arm, f.as_of FROM forecast_outcomes o JOIN forecasts f ON f.forecast_id=o.forecast_id "
                                        "WHERE o.scored_at>=? ORDER BY o.horizon, f.symbol", (run.get("started_at") or "",))]
    health = store.health_events_for_run(run_id)
    arm_curves = {a: store.arm_history(a, mode) for a in ("A", "B", "C")}
    exposure = [{"date": h["date"], "equity": h["equity"], "gross": (sum(p.get("value", 0) for p in json.loads(h["positions_json"] or "{}").values()) / h["equity"]) if h.get("equity") else None}
                for h in hist]
    risk_art = art("risk")
    v2 = art("v2_research") or {}
    research = {k: v2.get(k) for k in ("extraction", "candidates", "candidate_reasons", "degraded", "deadline_hit", "timings", "source_coverage", "n_events", "events_routed")}
    events = [e for e in (v2.get("events") or [])][:40]
    return {"run": run, "stages": stages, "validation": art("validate"), "regime": art("features").get("regime"),
            "ai": art("ai"), "proposal": art("propose"), "risk": risk_art, "execution": art("execute"),
            "reconcile": art("reconcile") or art("reconcile_only"), "account": art("account"), "decisions": decisions[:25],
            "orders": orders, "model_calls": calls, "metrics": metrics, "costs": costs,
            "safe_mode": store.safe_mode_active(),
            "arms": arms, "divergence": div, "forecasts": fcs, "matured_outcomes": matured[:40], "health": health,
            "arm_curves": arm_curves, "exposure_series": exposure, "research": research, "events": events,
            "run_outcome": (art("summary") or {}).get("run_outcome") or run.get("status"),
            "summary": art("summary") or {}}


def write_reports(rep: Dict[str, Any], out_dir: Path, as_of: str, run_id: str, html_on: bool = True,
                  json_on: bool = True, run_date: Optional[str] = None) -> Dict[str, str]:
    """Filenames use the invocation date (run_date) and the run outcome; decision as_of is inside."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}
    outcome = str(rep.get("run_outcome") or rep["run"].get("status") or "unknown")
    stem = f"{run_date or as_of}_{outcome}_{run_id}"
    if json_on:
        p = out_dir / f"{stem}.json"
        p.write_text(json.dumps(rep, indent=1, default=str))
        (out_dir / "latest.json").write_text(json.dumps(rep, indent=1, default=str))
        paths["json"] = str(p)
    if html_on:
        p = out_dir / f"{stem}.html"
        doc = render_html(rep)
        p.write_text(doc)
        (out_dir / "latest.html").write_text(doc)
        paths["html"] = str(p)
    return paths


def _fmt(v: Any, pct: bool = False) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "–"
    if isinstance(v, float):
        return f"{v:.2%}" if pct else f"{v:.4f}"
    return html.escape(str(v))


def _table(rows: List[Dict[str, Any]], cols: List[str], pct_cols: set = frozenset()) -> str:
    if not rows:
        return "<p class=muted>none</p>"
    h = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    b = "".join("<tr>" + "".join(f"<td>{_fmt(r.get(c), c in pct_cols)}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def _svg_line(series: Dict[str, List[float]], labels: List[str], title: str, w: int = 720, h: int = 220, pct: bool = False) -> str:
    """Inline SVG multi-line chart; no external libraries (artifact-safe, mail-safe)."""
    series = {k: [v for v in vs if v is not None] for k, vs in series.items()}
    series = {k: vs for k, vs in series.items() if len(vs) >= 2}
    if not series:
        return f"<p class=muted>{html.escape(title)}: not enough data</p>"
    allv = [v for vs in series.values() for v in vs]
    lo, hi = min(allv), max(allv)
    if hi - lo < 1e-9:
        hi = lo + 1e-9
    pad = 36
    def X(i, L): return pad + (w - 2 * pad) * (i / max(L - 1, 1))
    def Y(v): return h - pad - (h - 2 * pad) * ((v - lo) / (hi - lo))
    colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e"]
    out = [f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}' style='font:11px system-ui;background:#fafafa;border:1px solid #ddd'>",
           f"<text x='{pad}' y='14' font-weight='bold'>{html.escape(title)}</text>",
           f"<text x='2' y='{Y(hi)+4}' fill='#666'>{hi:.1%}</text>" if pct else f"<text x='2' y='{Y(hi)+4}' fill='#666'>{hi:.0f}</text>",
           f"<text x='2' y='{Y(lo)+4}' fill='#666'>{lo:.1%}</text>" if pct else f"<text x='2' y='{Y(lo)+4}' fill='#666'>{lo:.0f}</text>",
           f"<line x1='{pad}' y1='{h-pad}' x2='{w-pad}' y2='{h-pad}' stroke='#999'/>"]
    for i, (name, vs) in enumerate(series.items()):
        pts = " ".join(f"{X(j, len(vs)):.1f},{Y(v):.1f}" for j, v in enumerate(vs))
        out.append(f"<polyline fill='none' stroke='{colors[i % len(colors)]}' stroke-width='1.6' points='{pts}'/>")
        out.append(f"<text x='{w-pad-90}' y='{28+12*i}' fill='{colors[i % len(colors)]}'>{html.escape(name)}</text>")
    if labels:
        out.append(f"<text x='{pad}' y='{h-4}' fill='#666'>{html.escape(labels[0])}</text>")
        out.append(f"<text x='{w-pad-60}' y='{h-4}' fill='#666'>{html.escape(labels[-1])}</text>")
    out.append("</svg>")
    return "".join(out)


def render_html(rep: Dict[str, Any]) -> str:
    run, m, c = rep["run"], rep["metrics"], rep["costs"]
    acct = rep.get("account") or {}
    risk = rep.get("risk") or {}
    ex = rep.get("execution") or {}
    ai = rep.get("ai") or {}
    reg = rep.get("regime") or {}
    sm = rep.get("safe_mode")
    stage_rows = [{"stage": k, "status": v["status"], "error": v["error"]} for k, v in rep["stages"].items()]
    dec_rows = [{**d, "risk_flags": d.get("risk_flags_json")} for d in rep["decisions"]]
    css = ("body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}"
           "table{border-collapse:collapse;font-size:13px;margin:.5rem 0}th,td{border:1px solid #ddd;padding:3px 6px;text-align:right}"
           "th:first-child,td:first-child{text-align:left}h2{margin-top:1.6rem;border-bottom:1px solid #ccc}"
           ".kpi{display:inline-block;margin:0 1.2rem .6rem 0}.kpi b{display:block;font-size:1.2rem}.muted{color:#888}"
           ".warn{background:#fff3cd;padding:.5rem;border:1px solid #ffeeba}.bad{background:#f8d7da;padding:.5rem;border:1px solid #f5c6cb}")
    kpi = lambda label, v, pct=False: f"<div class=kpi><span class=muted>{label}</span><b>{_fmt(v, pct)}</b></div>"  # noqa: E731
    summ = rep.get("summary") or {}
    parts = [f"<!doctype html><meta charset=utf-8><title>Sea Lion {run.get('as_of')} {run.get('mode')}</title><style>{css}</style>",
             f"<h1>Sea Lion daily report <small class=muted>({run.get('mode')}, run {run.get('run_id')})</small></h1>",
             f"<p><b>run_date</b> {summ.get('run_date', run.get('started_at', '')[:10])} · <b>decision_as_of</b> {run.get('as_of')} · "
             f"<b>run_outcome</b> {rep.get('run_outcome')} · status {run.get('status')} · strategy {run.get('strategy_version')} · "
             f"config {run.get('config_hash')} · code {run.get('code_version')}</p>"]
    if summ:
        parts.append("<p class=muted>" + html.escape(" · ".join(f"{k}={v}" for k, v in summ.items() if k not in ("run_date",))) + "</p>")
    if rep.get("health"):
        parts.append("<h2>Health events</h2>" + _table(rep["health"], ["created_at", "severity", "component", "reason"]))
    if sm:
        parts.append(f"<div class=bad><b>SAFE MODE ACTIVE</b> since {sm['activated_at']}: {html.escape(sm['reason'])}. "
                     "New exposure is blocked until cleared manually (<code>sea-lion safe-mode --clear</code>).</div>")
    if run.get("error"):
        parts.append(f"<div class=bad>error: {html.escape(str(run['error']))}</div>")
    parts.append("<h2>Account</h2>" + kpi("equity", acct.get("equity")) + kpi("cash", acct.get("cash"))
                 + kpi("start-of-day equity", acct.get("start_of_day_equity")) + kpi("high-water mark", acct.get("high_water_mark"))
                 + kpi("day P&L", risk.get("stats", {}).get("day_pnl"), True) + kpi("drawdown", risk.get("stats", {}).get("drawdown"), True))
    parts.append("<h2>Performance (strategy vs benchmark)</h2>" + kpi("days", m.get("n_days")) + kpi("total return", m.get("total_return"), True)
                 + kpi("benchmark (SPY)", m.get("benchmark_return"), True) + kpi("excess", m.get("excess_return"), True)
                 + kpi("ann. vol", m.get("ann_vol"), True) + kpi("Sharpe", m.get("sharpe")) + kpi("max drawdown", m.get("max_drawdown"), True)
                 + kpi("hit rate", m.get("hit_rate"), True))
    if m.get("small_sample_warning", True):
        parts.append("<p class=warn>Small sample: fewer than 60 sessions. Treat every statistic above as noise until proven otherwise.</p>")
    # ---- V2: equity + exposure charts, arms, divergence, forecasts, events
    hist_dates = [e["date"] for e in rep.get("exposure_series", [])]
    eq_series = {"orders arm (broker)": [e["equity"] for e in rep.get("exposure_series", [])]}
    for a, curve in (rep.get("arm_curves") or {}).items():
        if curve:
            eq_series[f"arm {a} (shadow sim)"] = [x["sim_equity"] for x in curve]
    parts.append("<h2>Equity and exposure</h2>" + _svg_line(eq_series, hist_dates, f"Equity ({len(hist_dates)} sessions; small sample)")
                 + _svg_line({"realized gross": [e["gross"] for e in rep.get("exposure_series", [])],
                              "target gross": [risk.get("stats", {}).get("gross_target")] * len(hist_dates)}, hist_dates,
                             "Realized vs target exposure", pct=True))
    book = (risk.get("stats") or {}).get("book") or {}
    if book:
        parts.append("<p>book: positions " + str(book.get("n_positions")) + " · gross actual " + _fmt(book.get("gross_actual"), True)
                     + " · dust " + html.escape(", ".join(book.get("dust", [])) or "none") + " · sector actual " + html.escape(json.dumps(book.get("sector_actual")))
                     + " · sector target " + html.escape(json.dumps(book.get("sector_target"))) + " · target beta " + _fmt(book.get("beta_target")) + "</p>")
    if risk.get("stats", {}).get("scenarios"):
        parts.append("<p>scenarios (fraction of equity): " + html.escape(json.dumps(risk["stats"]["scenarios"])) + "</p>")
    arms = rep.get("arms") or {}
    if arms:
        rows = []
        for a, ar in sorted(arms.items()):
            rows.append({"arm": a, "orders": "yes" if ar.get("is_orders_arm") else "shadow", "candidates": ", ".join(ar.get("candidates") or []),
                         "gross": sum((ar.get("target_weights") or {}).values()), "sim_equity": ar.get("sim_equity")})
        parts.append("<h2>Arms (same cutoff)</h2>" + _table(rows, ["arm", "orders", "candidates", "gross", "sim_equity"]))
    div = rep.get("divergence") or {}
    if div:
        rows = [{"arm": a, "vs": d["baseline_arm"], "rank_changes": len(d.get("rank_changes") or {}), "added": ", ".join(d.get("membership_added") or []),
                 "removed": ", ".join(d.get("membership_removed") or []), "weight_distance": d.get("weight_distance"),
                 "orders_caused": d.get("orders_caused"), "orders_prevented": d.get("orders_prevented"), "orders_resized": d.get("orders_resized"),
                 "notional_delta": d.get("notional_delta")} for a, d in sorted(div.items())]
        parts.append("<h2>Did the AI change the decision?</h2>" + _table(rows, ["arm", "vs", "rank_changes", "added", "removed", "weight_distance", "orders_caused", "orders_prevented", "orders_resized", "notional_delta"]))
    fcs = rep.get("forecasts") or []
    if fcs:
        rows = [{"arm": f["arm"], "symbol": f["symbol"], "abstain": f["abstain"], "p5": (f["calibrated"].get("5d") or {}).get("p_positive_excess_return"),
                 "p10": (f["calibrated"].get("10d") or {}).get("p_positive_excess_return"), "p20": (f["calibrated"].get("20d") or {}).get("p_positive_excess_return"),
                 "evidence_quality": f["evidence_quality"], "disagreement": f["model_disagreement"], "overlay": f["overlay_score"],
                 "evidence": len(f["evidence_ids"]), "flags": ", ".join(f["risk_flags"]), "reason": f.get("reason", "")} for f in fcs]
        parts.append("<h2>Forecasts (frozen at decision time)</h2>" + _table(rows, ["arm", "symbol", "abstain", "p5", "p10", "p20", "evidence_quality", "disagreement", "overlay", "evidence", "flags", "reason"]))
    if rep.get("matured_outcomes"):
        parts.append("<h2>Forecasts reaching a horizon today</h2>" + _table(rep["matured_outcomes"], ["symbol", "arm", "as_of", "horizon", "p_positive", "excess_return", "hit", "brier"], {"excess_return"}))
    res = rep.get("research") or {}
    if res.get("extraction") or res.get("n_events"):
        parts.append("<h2>V2 research</h2><pre>" + html.escape(json.dumps(res, indent=1, default=str)[:3000]) + "</pre>")
    evs = rep.get("events") or []
    if evs:
        rows = [{"symbol": e.get("primary_symbol"), "type": e.get("event_type"), "class": e.get("routing_class"), "actionable": e.get("actionable"),
                 "importance": e.get("importance"), "novelty": e.get("novelty"), "quality": e.get("source_quality"), "contradiction": e.get("contradiction_level"),
                 "docs": len(e.get("document_ids") or []), "title": (e.get("title") or "")[:90], "reason": e.get("routing_reason")} for e in evs]
        parts.append("<h2>Canonical events</h2>" + _table(rows, ["symbol", "type", "class", "actionable", "importance", "novelty", "quality", "contradiction", "docs", "title", "reason"]))
    parts.append(f"<h2>Regime</h2><p>{html.escape(json.dumps(reg))}</p>")
    parts.append("<h2>AI layer</h2><pre>" + html.escape(json.dumps(ai, indent=1, default=str)[:4000]) + "</pre>")
    parts.append("<h2>Decisions (top 25)</h2>" + _table(dec_rows, ["symbol", "rank", "quant_score", "ai_score", "ensemble_score", "confidence", "proposed_weight", "approved_weight", "risk_flags"]))
    parts.append("<h2>Risk engine</h2><p>flags: " + html.escape(", ".join(risk.get("global_flags", [])) or "none")
                 + f" · block_new_exposure={risk.get('block_new_exposure')}</p>"
                 + _table([{"symbol": d["symbol"], "requested": d["requested_weight"], "approved": d["approved_weight"], "reasons": ", ".join(d["reasons"])}
                           for d in risk.get("decisions", []) if d.get("reasons")], ["symbol", "requested", "approved", "reasons"]))
    parts.append("<h2>Orders</h2>" + _table(rep["orders"], ["symbol", "side", "qty", "limit_price", "notional", "status", "filled_qty", "filled_avg_price", "client_order_id"]))
    parts.append(f"<p>execution: submitted {len(ex.get('submitted', []))}, skipped {len(ex.get('skipped', []))}, errors {len(ex.get('errors', []))}, "
                 f"ambiguous {len(ex.get('ambiguous', []))}, duplicates prevented {ex.get('duplicates_prevented', 0)}</p>")
    parts.append("<h2>Reconciliation</h2><pre>" + html.escape(json.dumps(rep.get("reconcile"), default=str)[:2000]) + "</pre>")
    parts.append("<h2>Costs</h2>" + kpi("AI today (USD)", c["ai_today"]["usd"]) + kpi("AI tokens today", c["ai_today"]["tokens_in"] + c["ai_today"]["tokens_out"])
                 + kpi("AI month (USD)", c["ai_month"]["usd"]) + kpi("all costs month (USD)", c["all_month"]["usd"]))
    parts.append("<h2>Model calls</h2>" + _table(rep["model_calls"], ["tier", "provider", "model", "cache_hit", "input_tokens", "output_tokens", "latency_ms", "cost_usd", "valid", "error"]))
    parts.append("<h2>Pipeline stages</h2>" + _table(stage_rows, ["stage", "status", "error"]))
    parts.append("<h2>Data validation</h2><pre>" + html.escape(json.dumps(rep.get("validation"), default=str)[:3000]) + "</pre>")
    parts.append("<p class=muted>Not investment advice. Research software; paper results may not reproduce live.</p>")
    return "\n".join(parts)
