"""Daily pipeline orchestrator.

Stages (each writes an immutable artifact before the next starts):
  ingest -> validate -> account -> features -> ai -> propose -> risk -> execute -> report

- `run()`      : a fresh run for a decision date.
- `resume`     : same run_id; completed stages are loaded from their artifacts, so a crash
                 mid-run never re-submits (client_order_id embeds the run_id).
- `replay`     : new run that re-derives the proposal from the stored input snapshot and the
                 model cache only (no network, no orders) and compares it with the original.
"""
from __future__ import annotations

import logging
import subprocess
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from . import execution, features as F, reconcile as R, report as REP, risk as RK, strategy as S
from .ai.router import ModelRouter
from .broker.base import Broker, BrokerError
from .broker.sim import SimBroker, empty_state
from .config import REPO_ROOT, Settings
from .data.base import bars_hash
from .data.validate import validate_bars
from .store import Store, utcnow

log = logging.getLogger(__name__)

STAGES = ["ingest", "validate", "account", "features", "ai", "propose", "risk", "execute", "report"]


class Abort(Exception):
    """Controlled stop: the run ends with a non-error status and no orders."""

    def __init__(self, status: str, msg: str):
        super().__init__(msg)
        self.status = status


def code_version() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip() or "nogit"
    except Exception:  # noqa: BLE001
        return "nogit"


@dataclass
class Ctx:
    as_of: str
    bars: pd.DataFrame = field(default_factory=pd.DataFrame)
    events: List[Dict[str, Any]] = field(default_factory=list)
    validation: Dict[str, Any] = field(default_factory=dict)
    ok_symbols: List[str] = field(default_factory=list)
    account: Dict[str, Any] = field(default_factory=dict)
    scored: Optional[pd.DataFrame] = None
    regime: Dict[str, Any] = field(default_factory=dict)
    ai_scores: Dict[str, S.AIScore] = field(default_factory=dict)
    ai_info: Dict[str, Any] = field(default_factory=dict)
    proposal: Dict[str, Any] = field(default_factory=dict)
    risk: Optional[RK.RiskResult] = None
    execution: Dict[str, Any] = field(default_factory=dict)


class Pipeline:
    def __init__(self, cfg: Settings, store: Store, market_data=None, event_source=None, broker: Optional[Broker] = None):
        self.cfg = cfg
        self.store = store
        self._md = market_data
        self._es = event_source
        self._broker = broker
        self.mode = cfg.run.mode

    # ------------------------------------------------------------------ factories
    def market_data(self):
        if self._md is None:
            if self.cfg.data.provider == "alpaca":
                from .data.alpaca_source import AlpacaData
                k, s = self.cfg.alpaca_keys()
                self._md = AlpacaData(k, s)
            else:
                from .data.yfinance_source import YFinanceData
                self._md = YFinanceData()
        return self._md

    def event_source(self):
        if self._es is None:
            p = self.cfg.data.events_provider
            if p == "alpaca":
                from .data.alpaca_source import AlpacaNews
                k, s = self.cfg.alpaca_keys()
                self._es = AlpacaNews(k, s)
            elif p == "file":
                from .data.file_source import FileEvents
                self._es = FileEvents(self.cfg.data.events_file or "")
            elif p == "none":
                from .data.file_source import NoEvents
                self._es = NoEvents()
            else:
                from .data.yfinance_source import YFinanceNews
                self._es = YFinanceNews()
        return self._es

    def broker(self) -> Broker:
        if self._broker is None:
            if self.cfg.broker.provider == "alpaca" and self.mode in ("paper", "live"):
                from .broker.alpaca import AlpacaBroker
                k, s = self.cfg.alpaca_keys()
                self._broker = AlpacaBroker(k, s, paper=(self.mode != "live"))
            else:
                state = self.store.kv_get(f"sim_state:{self.mode}") or empty_state(self.cfg.broker.initial_cash)
                self._broker = SimBroker(state, self.cfg.broker.sim_slippage_bps, self.cfg.broker.sim_commission_usd,
                                         persist=lambda st: self.store.kv_set(f"sim_state:{self.mode}", st))
        return self._broker

    # ------------------------------------------------------------------ entry points
    def run(self, as_of: Optional[date] = None, resume_run_id: Optional[str] = None,
            replay_of: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        replay = replay_of is not None
        if resume_run_id:
            row = self.store.run(resume_run_id)
            if not row:
                raise ValueError(f"unknown run {resume_run_id}")
            run_id, as_of_s = resume_run_id, row["as_of"]
        else:
            if replay:
                orig = self.store.run(replay_of)
                if not orig:
                    raise ValueError(f"unknown run {replay_of}")
                as_of_s = orig["as_of"]
            else:
                as_of_s = (as_of or self._default_as_of()).isoformat()
                if not force and self.store.q1("SELECT 1 FROM runs WHERE mode=? AND as_of=? AND status='ok'", (self.mode, as_of_s)):
                    return {"run_id": None, "status": "skipped_already_completed", "as_of": as_of_s}
            run_id = f"{as_of_s}-{self.mode}-{uuid.uuid4().hex[:8]}" + ("-replay" if replay else "")
            self.store.create_run(run_id, self.mode, as_of_s, code_version(), self.cfg.config_hash,
                                  self.cfg.run.strategy_version, replay_of)
        ctx = Ctx(as_of=as_of_s)
        status, err = "ok", None
        try:
            self._stage(run_id, ctx, "ingest", self.st_ingest, replay_of=replay_of)
            self._stage(run_id, ctx, "validate", self.st_validate)
            self._stage(run_id, ctx, "account", self.st_account, replay_of=replay_of)
            self._stage(run_id, ctx, "features", self.st_features)
            self._stage(run_id, ctx, "ai", self.st_ai, replay=replay)
            self._stage(run_id, ctx, "propose", self.st_propose)
            self._stage(run_id, ctx, "risk", self.st_risk)
            self._stage(run_id, ctx, "execute", self.st_execute, replay=replay)
        except Abort as a:
            status, err = a.status, str(a)
            log.warning("run %s aborted: %s (%s)", run_id, a.status, a)
        except Exception as e:  # noqa: BLE001
            status, err = "error", f"{type(e).__name__}: {e}"
            log.error("run %s failed:\n%s", run_id, traceback.format_exc())
        self.store.finish_run(run_id, status, err)
        try:
            paths = self._report(run_id)
        except Exception as e:  # noqa: BLE001
            log.error("report failed: %s", e)
            paths = {}
        out = {"run_id": run_id, "status": status, "error": err, "as_of": as_of_s, "reports": paths}
        if replay:
            out["replay"] = self._compare_replay(replay_of, run_id)
        return out

    # ------------------------------------------------------------------ stage runner
    def _stage(self, run_id: str, ctx: Ctx, name: str, fn, **kw) -> None:
        art = self.store.stage_artifact(run_id, name)
        if art is not None:                         # resume: load, don't redo
            log.info("stage %s: loaded from artifact", name)
            self._load(name, ctx, art)
            return
        self.store.stage_start(run_id, name)
        try:
            artifact = fn(run_id, ctx, **kw)
        except Abort as a:
            self.store.stage_done(run_id, name, {"abort": a.status, "msg": str(a)}, status=a.status, error=str(a))
            raise
        except Exception as e:  # noqa: BLE001
            self.store.stage_done(run_id, name, None, status="error", error=f"{type(e).__name__}: {e}")
            raise
        self.store.stage_done(run_id, name, artifact)

    def _load(self, name: str, ctx: Ctx, art: Dict[str, Any]) -> None:
        """Rebuild ctx from a stored artifact (resume path)."""
        if name == "ingest":
            ctx.bars = pd.read_parquet(art["snapshot_path"])
            ctx.events = art["events"]
        elif name == "validate":
            ctx.validation, ctx.ok_symbols = art, art["ok_symbols"]
        elif name == "account":
            ctx.account = art
        elif name == "features":
            ctx.scored = pd.DataFrame(art["table"]).set_index("symbol") if art.get("table") else None
            ctx.regime = art["regime"]
        elif name == "ai":
            ctx.ai_info = art
            ctx.ai_scores = {s: S.AIScore(**v) for s, v in art.get("scores", {}).items()}
        elif name == "propose":
            ctx.proposal = art
            if ctx.scored is not None and art.get("table"):
                ctx.scored = pd.DataFrame(art["table"]).set_index("symbol")
        elif name == "risk":
            ctx.risk = RK.RiskResult(approved=art["approved"], intents=[RK.OrderIntent(**i) for i in art["intents"]],
                                     decisions=[RK.RiskDecision(**d) for d in art["decisions"]],
                                     global_flags=art["global_flags"], block_new_exposure=art["block_new_exposure"],
                                     enter_safe_mode=art.get("enter_safe_mode"), stats=art.get("stats", {}),
                                     held_for_review=[RK.OrderIntent(**i) for i in art.get("held_for_review", [])])
        elif name == "execute":
            ctx.execution = art

    # ------------------------------------------------------------------ stages
    def _default_as_of(self) -> date:
        """Decision date = the last COMPLETED session. Before 16:15 ET the current day's bar is
        still forming (Yahoo returns it as a partial bar), so use the previous calendar day and
        let ingest collapse it onto the last real bar."""
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.hour < 16 or (now_et.hour == 16 and now_et.minute < 15):
            return (now_et - timedelta(days=1)).date()
        return now_et.date()

    def st_ingest(self, run_id: str, ctx: Ctx, replay_of: Optional[str] = None) -> Dict[str, Any]:
        u = self.cfg.universe
        snap_dir = self.cfg.dir("data", "snapshots")
        if replay_of:
            orig = self.store.stage_artifact(replay_of, "ingest")
            if not orig:
                raise Abort("replay_missing_inputs", "original run has no ingest artifact")
            ctx.bars = pd.read_parquet(orig["snapshot_path"])
            ctx.events = orig["events"]
            art = dict(orig)
            art["replayed_from"] = replay_of
            return art
        as_of = date.fromisoformat(ctx.as_of)
        start = as_of - timedelta(days=self.cfg.data.lookback_days)
        md = self.market_data()
        bars = md.fetch_bars(u.tickers, start, as_of)
        if bars.empty:
            raise Abort("aborted_no_data", f"{md.name} returned no bars")
        bars = bars[bars["date"] <= ctx.as_of].reset_index(drop=True)
        latest = max(bars["date"])
        # Decide as of the latest completed session (weekend/holiday runs collapse onto the last bar).
        if latest != ctx.as_of:
            gap = (as_of - date.fromisoformat(latest)).days
            if gap > self.cfg.data.max_staleness_days:
                raise Abort("aborted_stale_data", f"latest bar {latest} is {gap}d older than {ctx.as_of} "
                                                  f"(max {self.cfg.data.max_staleness_days}d)")
            log.info("no bar for %s; using latest completed session %s as decision date", ctx.as_of, latest)
            ctx.as_of = latest
            self.store.x("UPDATE runs SET as_of=? WHERE run_id=?", (latest, run_id))
            if self.store.q1("SELECT 1 FROM runs WHERE mode=? AND as_of=? AND status='ok' AND run_id<>?",
                             (self.mode, latest, run_id)):
                raise Abort("skipped_already_completed", f"a run for session {latest} already completed")
        self.store.upsert_bars(bars.to_dict("records"), md.name)
        path = snap_dir / f"{run_id}.parquet"
        bars.to_parquet(path, index=False)
        # events
        es = self.event_source()
        since = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=self.cfg.data.events_lookback_days)
        try:
            evs = es.fetch_events(u.tickers, since, self.cfg.data.events_max_per_symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("event fetch failed (%s); continuing without events", e)
            evs = []
        rows = [e.to_row() for e in evs]
        self.store.upsert_events(run_id, rows)
        ctx.bars, ctx.events = bars, rows
        return {"source": md.name, "n_bars": int(len(bars)), "symbols": sorted(bars["symbol"].unique().tolist()),
                "latest_date": latest, "bars_hash": bars_hash(bars), "snapshot_path": str(path),
                "events_source": es.name, "n_events": len(rows), "events": rows, "fetched_at": utcnow()}

    def st_validate(self, run_id: str, ctx: Ctx) -> Dict[str, Any]:
        f = self.cfg.features
        min_rows = max(f.mom_long + f.skip_last_day + 5, f.trend_ma + 5)
        rep = validate_bars(ctx.bars, self.cfg.universe.tickers, self.cfg.universe.benchmark,
                            date.fromisoformat(ctx.as_of), min_rows, self.cfg.data.max_staleness_days)
        ctx.validation, ctx.ok_symbols = rep.to_dict(), rep.ok_symbols
        if rep.stale:
            raise Abort("aborted_stale_data", f"latest bar {rep.latest_date} older than {self.cfg.data.max_staleness_days}d")
        if not rep.benchmark_ok:
            raise Abort("aborted_benchmark_invalid", f"benchmark rejected: {rep.rejected.get(self.cfg.universe.benchmark)}")
        if not rep.ok:
            raise Abort("aborted_validation", "no valid symbols")
        return rep.to_dict()

    def st_account(self, run_id: str, ctx: Ctx, replay_of: Optional[str] = None) -> Dict[str, Any]:
        if replay_of:
            orig = self.store.stage_artifact(replay_of, "account")
            if not orig:
                raise Abort("replay_missing_inputs", "original run has no account artifact")
            ctx.account = dict(orig, replayed_from=replay_of)
            return ctx.account
        closes = ctx.bars[ctx.bars["date"] == ctx.as_of].set_index("symbol")["close"].to_dict()
        opens = ctx.bars[ctx.bars["date"] == ctx.as_of].set_index("symbol")["open"].to_dict()
        br = self.broker()
        recon: Dict[str, Any] = {}
        if isinstance(br, SimBroker):
            if br.s.get("date") != ctx.as_of:
                fills = br.settle(ctx.as_of, opens, closes)     # yesterday's orders fill at today's open
                recon = {"sim_fills": [f.client_order_id for f in fills]}
            else:
                br.mark(closes, ctx.as_of)
        if self.cfg.submits_orders:
            try:
                recon.update(R.reconcile(self.store, br, self.cfg, run_id))
            except BrokerError as e:
                raise Abort("aborted_broker_unavailable", f"reconcile failed: {e}")
        try:
            acct = br.account()
        except Exception as e:  # noqa: BLE001
            raise Abort("aborted_broker_unavailable", f"account fetch failed: {e}")
        prev = self.store.latest_equity(self.mode)
        sod = acct.last_equity if acct.last_equity else (prev["equity"] if prev and prev["date"] < ctx.as_of else acct.equity)
        hwm = max(float(self.store.kv_get(f"hwm:{self.mode}", 0.0) or 0.0), acct.equity)
        self.store.kv_set(f"hwm:{self.mode}", hwm)
        positions = {s: {"qty": p.qty, "avg_price": p.avg_price, "price": p.current_price, "value": p.market_value}
                     for s, p in acct.positions.items()}
        bench_close = closes.get(self.cfg.universe.benchmark)
        self.store.save_equity(ctx.as_of, self.mode, acct.equity, acct.cash, positions, hwm, sod, bench_close, br.name)
        safe = self.store.safe_mode_active()
        ctx.account = {"broker": br.name, "equity": acct.equity, "cash": acct.cash, "buying_power": acct.buying_power,
                       "positions": positions, "start_of_day_equity": sod, "high_water_mark": hwm,
                       "safe_mode": bool(safe), "safe_mode_reason": safe["reason"] if safe else None,
                       "reconcile": recon, "prices": closes, "benchmark_close": bench_close,
                       "broker_raw": acct.raw, "clock": br.clock()}
        return ctx.account

    def st_features(self, run_id: str, ctx: Ctx) -> Dict[str, Any]:
        bars = ctx.bars[ctx.bars["symbol"].isin(ctx.ok_symbols)]
        fp = F.compute_feature_panel(F.Panel.from_long(bars), self.cfg.features)
        feat = F.features_at(fp, ctx.as_of, ctx.ok_symbols)
        scored = S.quant_scores(feat, self.cfg.features, self.cfg.strategy)
        regime = F.market_regime(feat, self.cfg.universe.benchmark, self.cfg.strategy.regime)
        ctx.scored, ctx.regime = scored, regime
        table = scored.reset_index().to_dict("records")
        return {"regime": regime, "n_eligible": int(scored["eligible"].sum()), "table": table}

    def st_ai(self, run_id: str, ctx: Ctx, replay: bool = False) -> Dict[str, Any]:
        cfg = self.cfg.ai
        info: Dict[str, Any] = {"enabled": cfg.enabled, "available": False, "facts": [], "assessments": {}, "scores": {},
                                "main_candidates": [], "router": {}, "skipped_reason": None}
        ctx.ai_info = info
        if not cfg.enabled:
            info["skipped_reason"] = "disabled"
            return info
        if not ctx.events:
            info["skipped_reason"] = "no_new_events"
            info["available"] = True
            return info
        month_start = ctx.as_of[:7] + "-01"
        router = ModelRouter(cfg, self.store, run_id, ctx.as_of, month_start, ctx.ok_symbols, replay=replay)
        if router.state.budget_status == "hard_stop":
            info["skipped_reason"] = "budget_exhausted"
            info["router"] = router.state.to_dict()
            return info
        facts = router.extract_facts([e for e in ctx.events if e["symbol"] in ctx.ok_symbols])
        facts = [f for f in facts if f.relevant and not f.duplicate_of]
        info["facts"] = [f.model_dump() for f in facts]
        by_sym: Dict[str, list] = {}
        for f in facts:
            by_sym.setdefault(f.ticker, []).append(f)
        sc = ctx.scored
        ranked = sc[sc["eligible"].astype(bool)].sort_values("quant_score", ascending=False)
        top = [s for s in ranked.index if s in by_sym][: cfg.main_top_n]
        important = [s for s, fl in by_sym.items() if max(f.importance for f in fl) >= cfg.importance_threshold]
        cands = list(dict.fromkeys(top + important))[: cfg.main_top_n + 3]
        info["main_candidates"] = cands
        for sym in cands:
            row = sc.loc[sym]
            snapshot = {"as_of": ctx.as_of, "mom_20d": _r(row.get("mom_short")), "mom_60d": _r(row.get("mom_long")),
                        "trend_vs_ma50": _r(row.get("trend")), "realized_vol_20d": _r(row.get("vol")),
                        "volume_ratio": _r(row.get("volume_ratio")), "quant_score": _r(row.get("quant_score")),
                        "market_regime": ctx.regime.get("state")}
            out = router.assess(sym, snapshot, by_sym[sym])
            if out is None:
                info["assessments"][sym] = None
                continue
            info["assessments"][sym] = out.model_dump()
            info["scores"][sym] = {"score": out.impact, "confidence": out.confidence, "risk_flags": out.risk_flags,
                                   "evidence_ids": out.evidence_ids, "horizon_days": out.horizon_days}
        ctx.ai_scores = {s: S.AIScore(**v) for s, v in info["scores"].items()}
        info["router"] = router.state.to_dict()
        info["available"] = not router.state.provider_unavailable
        ctx.ai_info = info
        return info

    def st_propose(self, run_id: str, ctx: Ctx) -> Dict[str, Any]:
        scored = S.ensemble(ctx.scored, ctx.ai_scores, self.cfg.strategy, self.cfg.risk)
        weights, qweights, cands, qcands = S.build_proposal(scored, ctx.regime, self.cfg.strategy, self.cfg.risk)
        ctx.scored = scored
        rank = {s: i + 1 for i, s in enumerate(cands)}
        decisions = []
        for sym, row in scored.iterrows():
            a = ctx.ai_scores.get(sym)
            decisions.append({"symbol": sym, "as_of": ctx.as_of, "horizon_days": a.horizon_days if a else 5,
                              "quant_score": _r(row["quant_score"]), "ai_score": _r(row["ai_score"]),
                              "ensemble_score": _r(row["ensemble_score"]), "confidence": _r(row["ai_confidence"]),
                              "risk_flags": a.risk_flags if a else [], "evidence_ids": a.evidence_ids if a else [],
                              "prompt_version": self.cfg.ai.prompt_version, "rank": rank.get(sym),
                              "proposed_weight": weights.get(sym, 0.0), "eligible": bool(row["eligible"]),
                              "features": {k: _r(row.get(k)) for k in ["mom_short", "mom_long", "trend", "vol", "volume_ratio"]}})
        self.store.save_decisions(run_id, decisions)
        ctx.proposal = {"weights": weights, "quant_only_weights": qweights, "candidates": cands,
                        "quant_only_candidates": qcands, "regime": ctx.regime,
                        "ai_contribution": {s: _r(scored.loc[s, "ai_score"] * self.cfg.strategy.ai_weight_cap) for s in cands},
                        "table": scored.reset_index().to_dict("records")}
        return ctx.proposal

    def st_risk(self, run_id: str, ctx: Ctx) -> Dict[str, Any]:
        a = ctx.account
        acct = RK.AccountState(equity=a["equity"], cash=a["cash"],
                               positions={s: p["qty"] for s, p in a["positions"].items()},
                               prices=a["prices"], start_of_day_equity=a["start_of_day_equity"],
                               high_water_mark=a["high_water_mark"], data_stale=bool(ctx.validation.get("stale")),
                               safe_mode=bool(a.get("safe_mode")), market_open_next_session=True)
        res = RK.evaluate(ctx.proposal["weights"], acct, self.cfg.universe.symbols, self.cfg.risk,
                          rank_order=ctx.proposal["candidates"])
        if res.enter_safe_mode:
            self.store.enter_safe_mode(res.enter_safe_mode, run_id)
        ai_info = ctx.ai_info or {}
        ai_needed = self.cfg.ai.enabled and ai_info.get("skipped_reason") not in ("no_new_events", "disabled")
        if ai_needed and not ai_info.get("available", False):
            res.global_flags.append("ai_unavailable_quant_only")
            if self.mode not in self.cfg.ai.quant_only_orders_allowed_modes:
                res.global_flags.append("ai_unavailable_orders_blocked")
                res.held_for_review.extend(res.intents)
                res.intents = []
        ctx.risk = res
        # persist final approved weights (after turnover / cash scaling) on the decision rows
        final = dict(res.approved)
        for d in res.decisions:
            final[d.symbol] = d.approved_weight
        with self.store.tx() as c:
            for s, w in final.items():
                c.execute("UPDATE decisions SET approved_weight=? WHERE run_id=? AND symbol=?", (w, run_id, s))
        return res.to_dict()

    def st_execute(self, run_id: str, ctx: Ctx, replay: bool = False) -> Dict[str, Any]:
        res = ctx.risk
        if replay:
            return {"mode": "replay", "orders": "disabled", "would_submit": [i.__dict__ for i in res.intents]}
        if not self.cfg.submits_orders:
            return {"mode": self.mode, "orders": "disabled", "would_submit": [i.__dict__ for i in res.intents]}
        if self.mode == "live":
            self._live_gate()
        if not res.intents:
            return {"mode": self.mode, "submitted": [], "skipped": [], "errors": [], "ambiguous": [], "duplicates_prevented": 0,
                    "note": "no intents"}
        br = self.broker()
        if isinstance(br, SimBroker):
            fresh = ctx.account["prices"]
            market_open_next = True
        else:
            fresh = self._fresh_prices([i.symbol for i in res.intents]) or ctx.account["prices"]
            clock = ctx.account.get("clock") or {}
            market_open_next = bool(clock.get("is_open") or clock.get("next_open"))
        rep = execution.execute(res.intents, br, self.store, self.cfg, run_id, fresh, market_open_next)
        if rep.ambiguous:
            self.store.enter_safe_mode(f"ambiguous broker response for {[a['symbol'] for a in rep.ambiguous]}", run_id)
        ctx.execution = rep.to_dict()
        return ctx.execution

    def _fresh_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Latest trade from Alpaca when keys exist (works after hours too), else the data adapter."""
        k, sec = self.cfg.alpaca_keys()
        if k and sec:
            try:
                from .data.alpaca_source import AlpacaData
                return AlpacaData(k, sec).latest_prices(symbols)
            except Exception as e:  # noqa: BLE001
                log.warning("alpaca latest-trade fetch failed (%s)", e)
        try:
            return self.market_data().latest_prices(symbols)
        except Exception as e:  # noqa: BLE001
            log.warning("fresh price fetch failed (%s); using close", e)
            return {}

    def _live_gate(self) -> None:
        import os
        if os.environ.get("SEA_LION_LIVE_ENABLED") != "1":
            raise Abort("live_blocked", "SEA_LION_LIVE_ENABLED != 1")
        expected = f"LIVE-{datetime.now(timezone.utc).date().isoformat()}"
        if os.environ.get("SEA_LION_LIVE_CONFIRM_TOKEN") != expected:
            raise Abort("live_blocked", f"manual confirmation token must equal {expected} (set daily)")
        paper_db = Path(self.cfg.runtime_dir) / "db" / "sea_lion_paper.db"
        n = Store(paper_db).completed_sessions("paper") if paper_db.exists() else 0
        if n < self.cfg.broker.paper_sessions_required_before_live:
            raise Abort("live_blocked", f"only {n} paper sessions completed; need {self.cfg.broker.paper_sessions_required_before_live}")

    def _report(self, run_id: str) -> Dict[str, str]:
        rep = REP.build_report(self.store, run_id, self.mode)
        as_of = rep["run"].get("as_of", "unknown")
        return REP.write_reports(rep, self.cfg.dir("reports", self.mode), as_of, run_id,
                                 self.cfg.reporting.write_html, self.cfg.reporting.write_json)

    def _compare_replay(self, orig_id: str, new_id: str) -> Dict[str, Any]:
        a = self.store.stage_artifact(orig_id, "propose") or {}
        b = self.store.stage_artifact(new_id, "propose") or {}
        wa, wb = a.get("weights", {}), b.get("weights", {})
        diffs = {s: (wa.get(s), wb.get(s)) for s in set(wa) | set(wb)
                 if abs((wa.get(s) or 0) - (wb.get(s) or 0)) > 1e-6}
        return {"identical": not diffs and bool(a) and bool(b), "diffs": diffs,
                "orig_candidates": a.get("candidates"), "new_candidates": b.get("candidates")}


def _r(v: Any, nd: int = 6) -> Optional[float]:
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None
