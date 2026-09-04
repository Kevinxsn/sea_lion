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
    return {"run": run, "stages": stages, "validation": art("validate"), "regime": art("features").get("regime"),
            "ai": art("ai"), "proposal": art("propose"), "risk": art("risk"), "execution": art("execute"),
            "reconcile": art("reconcile"), "account": art("account"), "decisions": decisions[:25],
            "orders": orders, "model_calls": calls, "metrics": metrics, "costs": costs,
            "safe_mode": store.safe_mode_active()}


def write_reports(rep: Dict[str, Any], out_dir: Path, as_of: str, run_id: str, html_on: bool = True,
                  json_on: bool = True) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    if json_on:
        p = out_dir / f"{as_of}_{run_id}.json"
        p.write_text(json.dumps(rep, indent=1, default=str))
        (out_dir / "latest.json").write_text(json.dumps(rep, indent=1, default=str))
        paths["json"] = str(p)
    if html_on:
        p = out_dir / f"{as_of}_{run_id}.html"
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
    parts = [f"<!doctype html><meta charset=utf-8><title>Sea Lion {run.get('as_of')} {run.get('mode')}</title><style>{css}</style>",
             f"<h1>Sea Lion daily report — {run.get('as_of')} <small class=muted>({run.get('mode')}, run {run.get('run_id')})</small></h1>",
             f"<p>status: <b>{run.get('status')}</b> · strategy {run.get('strategy_version')} · config {run.get('config_hash')} · code {run.get('code_version')}</p>"]
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
