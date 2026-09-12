"""Daily pipeline orchestrator, V2.

Two entry points share the stage machinery:
  research(as_of)  after-close job: bars, documents (news, SEC, FRED, fundamentals), features,
                   V1 headline AI (arm B) and V2 multi-pass research (arm C). Immutable, cached.
  run(as_of)       morning job: reconcile FIRST, reuse the research snapshot, ingest the overnight
                   delta, build arms A/B/C, apply risk to the orders arm, submit inside the
                   decision window, simulate the shadow arms, freeze forecasts, score matured
                   outcomes, write the summary/report, notify on attention states.
Stage artifacts are immutable; resume/replay work as in V1 (client_order_id embeds the run id)."""
from __future__ import annotations

import logging
import subprocess
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from . import arms as ARMS, execution, features as F, forecasts as FC, portfolio as PF, reconcile as R, report as REP
from . import risk as RK, strategy as S
from .ai.research import ResearchPipeline
from .ai.router import ModelRouter
from .broker.base import Broker, BrokerError
from .broker.sim import SimBroker, empty_state
from .calendar import ET, TradingCalendar
from .config import REPO_ROOT, Settings
from .data.base import bars_hash
from .data.documents import DocumentStore, SourceDocument, now_utc
from .data.validate import validate_bars
from .events.entities import EntityRegistry
from .notify import Notifier, summary_line
from .store import Store, utcnow

log = logging.getLogger(__name__)

STAGES = ["ingest", "validate", "account", "features", "ai", "v2_research", "propose", "risk", "execute", "summary"]
ATTENTION_FLAGS = {"safe_mode", "error", "ambiguous_orders", "reconcile_mismatch", "ai_unavailable_paper_live", "deadline_exceeded",
                   "decision_window_missed", "aborted", "margin_account", "primary_source_failed"}


class Abort(Exception):
    def __init__(self, status: str, msg: str):
        super().__init__(msg)
        self.status = status


def code_version() -> str:
    try:
        v = subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "diff", "--quiet"], capture_output=True).returncode != 0
        return (v or "nogit") + ("-dirty" if v and dirty else "")
    except Exception:  # noqa: BLE001
        return "nogit"


@dataclass
class Ctx:
    as_of: str
    run_date: str = ""
    cutoff: str = ""                       # ISO UTC: no document after this may affect the decision
    t_start: float = field(default_factory=time.time)
    bars: pd.DataFrame = field(default_factory=pd.DataFrame)
    events: List[Dict[str, Any]] = field(default_factory=list)      # V1-style event rows (arm B)
    documents: List[Dict[str, Any]] = field(default_factory=list)   # V2 source documents (arm C)
    doc_stats: Dict[str, Any] = field(default_factory=dict)
    validation: Dict[str, Any] = field(default_factory=dict)
    ok_symbols: List[str] = field(default_factory=list)
    account: Dict[str, Any] = field(default_factory=dict)
    scored: Optional[pd.DataFrame] = None
    feature_panel: Optional[Dict[str, pd.DataFrame]] = None
    regime: Dict[str, Any] = field(default_factory=dict)
    macro: Dict[str, Any] = field(default_factory=dict)
    fundamentals: Dict[str, Any] = field(default_factory=dict)
    ai_scores: Dict[str, S.AIScore] = field(default_factory=dict)           # arm B
    ai_info: Dict[str, Any] = field(default_factory=dict)
    v2: Dict[str, Any] = field(default_factory=dict)                        # arm C research artifact
    v2_scores: Dict[str, S.AIScore] = field(default_factory=dict)
    proposal: Dict[str, Any] = field(default_factory=dict)
    arms: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    risk: Optional[RK.RiskResult] = None
    execution: Dict[str, Any] = field(default_factory=dict)
    attention: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, cfg: Settings, store: Store, market_data=None, event_source=None, broker: Optional[Broker] = None,
                 doc_sources: Optional[List[Any]] = None, calendar: Optional[TradingCalendar] = None,
                 notifier: Optional[Notifier] = None):
        self.cfg = cfg
        self.store = store
        self._md = market_data
        self._es = event_source
        self._broker = broker
        self._doc_sources = doc_sources
        self._cal = calendar
        self._notifier = notifier
        self.mode = cfg.run.mode
        self.docs = DocumentStore(store, cfg.dir("data", "docs"))
        self._registry: Optional[EntityRegistry] = None
        self._edgar = None

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

    def doc_sources(self) -> List[Any]:
        """V2 document sources in priority order. Injected in tests; built from config otherwise."""
        if self._doc_sources is not None:
            return self._doc_sources
        srcs: List[Any] = []
        sc = self.cfg.v2.sources
        k, s = self.cfg.alpaca_keys()
        if sc.news_primary == "alpaca" and k and s:
            from .data.alpaca_source import AlpacaNewsDocuments
            srcs.append(AlpacaNewsDocuments(k, s, sc.news_include_content, sc.news_max_content_chars))
        if sc.news_fallback == "yfinance" and (sc.news_primary != "alpaca" or not (k and s)):
            from .data.yfinance_source import YFinanceNewsDocuments
            srcs.append(YFinanceNewsDocuments())
        self._doc_sources = srcs
        return srcs

    def edgar(self):
        if self._edgar is None and self.cfg.v2.sources.sec_enabled:
            from .data.edgar import Edgar
            self._edgar = Edgar(self.cfg.v2.sources.sec_user_agent, self.cfg.dir("data", "sec"))
        return self._edgar

    def registry(self) -> EntityRegistry:
        if self._registry is None:
            cik = {}
            try:
                if self.edgar():
                    cik = self.edgar().cik_map()
            except Exception as e:  # noqa: BLE001
                log.warning("CIK map unavailable: %s", e)
            self._registry = EntityRegistry(self.cfg.universe.symbols, cik)
        return self._registry

    def calendar(self) -> TradingCalendar:
        if self._cal is None:
            k, s = self.cfg.alpaca_keys()
            prov = self.cfg.v2.calendar.provider if (k and s) else "fallback"
            self._cal = TradingCalendar(self.store, prov, k, s, paper=(self.mode != "live"), cache_days=self.cfg.v2.calendar.cache_days)
        return self._cal

    def notifier(self) -> Notifier:
        if self._notifier is None:
            self._notifier = Notifier(self.cfg.notify, self.store, self.mode)
        return self._notifier

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

    # ------------------------------------------------------------------ entry: morning decision
    def run(self, as_of: Optional[date] = None, resume_run_id: Optional[str] = None, replay_of: Optional[str] = None,
            force: bool = False, dry_run: bool = False) -> Dict[str, Any]:
        replay = replay_of is not None
        run_date = datetime.now(ET).date().isoformat()
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
                as_of_s = (as_of.isoformat() if as_of else self._default_as_of())
            run_id = f"{as_of_s}-{self.mode}-{uuid.uuid4().hex[:8]}" + ("-replay" if replay else "")
            self.store.create_run(run_id, self.mode, as_of_s, code_version(), self.cfg.config_hash, self.cfg.run.strategy_version, replay_of)
        ctx = Ctx(as_of=as_of_s, run_date=run_date, cutoff=now_utc().isoformat(timespec="seconds"))
        if replay:
            orig_cut = (self.store.stage_artifact(replay_of, "summary") or {}).get("cutoff")
            ctx.cutoff = orig_cut or ctx.cutoff
        status, err, outcome = "ok", None, "completed"
        try:
            if not force and not replay and not resume_run_id and \
                    self.store.q1("SELECT 1 FROM runs WHERE mode=? AND as_of=? AND status='ok'", (self.mode, as_of_s)):
                raise Abort("skipped_already_completed", f"a run for session {as_of_s} already completed")
            self._stage(run_id, ctx, "ingest", self.st_ingest, replay_of=replay_of)
            self._stage(run_id, ctx, "validate", self.st_validate)
            self._stage(run_id, ctx, "account", self.st_account, replay_of=replay_of)
            self._stage(run_id, ctx, "features", self.st_features)
            self._stage(run_id, ctx, "ai", self.st_ai, replay=replay)
            self._stage(run_id, ctx, "v2_research", self.st_v2_research, replay=replay)
            self._stage(run_id, ctx, "propose", self.st_propose)
            self._stage(run_id, ctx, "risk", self.st_risk)
            self._stage(run_id, ctx, "execute", self.st_execute, replay=replay, dry_run=dry_run)
            if dry_run:
                outcome = "dry_run"
        except Abort as a:
            status, err = a.status, str(a)
            log.warning("run %s aborted: %s (%s)", run_id, a.status, a)
            if a.status == "skipped_already_completed":
                outcome = "reconcile_only"
                self._reconcile_only(run_id, ctx)
            else:
                outcome = "aborted"
                if a.status not in ("live_blocked",):
                    ctx.attention.append("aborted")
                    self.notifier().event("warning", "pipeline", f"run aborted: {a.status}: {a}", run_id)
        except Exception as e:  # noqa: BLE001
            status, err, outcome = "error", f"{type(e).__name__}: {e}", "error"
            log.error("run %s failed:\n%s", run_id, traceback.format_exc())
            ctx.attention.append("error")
            self.notifier().event("critical", "pipeline", f"run error: {err}", run_id, remediation="inspect runtime/logs/sea_lion.log; `sea-lion resume <run_id>` is safe")
        if self.store.safe_mode_active():
            outcome = "safe_mode" if outcome in ("completed", "reconcile_only", "dry_run") else outcome
            ctx.attention.append("safe_mode")
        self.store.finish_run(run_id, status, err)
        summary = self._summary(run_id, ctx, status, outcome, err)
        try:
            self._score_outcomes(ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("outcome scoring failed: %s", e)
        try:
            paths = self._report(run_id, ctx.run_date)
        except Exception as e:  # noqa: BLE001
            log.error("report failed: %s", e)
            paths = {}
        out = {"run_id": run_id, "status": status, "error": err, "as_of": ctx.as_of, "run_outcome": outcome, "reports": paths,
               "summary": summary, "summary_line": summary_line(summary)}
        if replay:
            out["replay"] = self._compare_replay(replay_of, run_id)
        return out

    # ------------------------------------------------------------------ entry: after-close research
    def research(self, as_of: Optional[date] = None, force: bool = False) -> Dict[str, Any]:
        as_of_s = as_of.isoformat() if as_of else self.calendar().last_completed_session()
        prev = self.store.research_run(as_of_s)
        if prev and prev["status"] == "ok" and not force:
            return {"run_id": prev["run_id"], "status": "exists", "as_of": as_of_s}
        run_id = f"{as_of_s}-research-{uuid.uuid4().hex[:8]}"
        self.store.create_run(run_id, "research", as_of_s, code_version(), self.cfg.config_hash, self.cfg.run.strategy_version)
        ctx = Ctx(as_of=as_of_s, run_date=datetime.now(ET).date().isoformat(), cutoff=now_utc().isoformat(timespec="seconds"))
        status, err = "ok", None
        try:
            self._stage(run_id, ctx, "ingest", self.st_ingest, research=True)
            self._stage(run_id, ctx, "validate", self.st_validate)
            self._stage(run_id, ctx, "features", self.st_features)
            self._stage(run_id, ctx, "ai", self.st_ai)
            self._stage(run_id, ctx, "v2_research", self.st_v2_research, full=True)
        except Abort as a:
            status, err = a.status, str(a)
        except Exception as e:  # noqa: BLE001
            status, err = "error", f"{type(e).__name__}: {e}"
            log.error("research %s failed:\n%s", run_id, traceback.format_exc())
            self.notifier().event("warning", "research", f"after-close research failed: {err}", run_id)
        self.store.finish_run(run_id, status, err)
        watermark = {"docs_until": max([d["available_at"] for d in ctx.documents] or [ctx.cutoff]), "cutoff": ctx.cutoff}
        self.store.save_research_run(as_of_s, run_id, status, watermark)
        try:
            self._score_outcomes(ctx)
            for h in self.cfg.v2.research.horizons:
                FC.maybe_fit(self.store, h, self.cfg.v2.calibration_min_samples, self.cfg.v2.forecast_version)
        except Exception as e:  # noqa: BLE001
            log.warning("outcome scoring failed: %s", e)
        summ = {"run_date": ctx.run_date, "decision_as_of": as_of_s, "run_outcome": "research" if status == "ok" else status,
                "status": status, "cutoff": ctx.cutoff, "n_documents": len(ctx.documents), "doc_stats": ctx.doc_stats,
                "v2": {k: ctx.v2.get(k) for k in ("candidates", "degraded", "deadline_hit", "timings", "extraction")},
                "ai_available": ctx.ai_info.get("available"), "elapsed_sec": round(time.time() - ctx.t_start)}
        self.store.stage_start(run_id, "summary"); self.store.stage_done(run_id, "summary", summ)
        return {"run_id": run_id, "status": status, "error": err, "as_of": as_of_s, "summary": summ, "summary_line": summary_line(summ)}

    # ------------------------------------------------------------------ stage runner
    def _stage(self, run_id: str, ctx: Ctx, name: str, fn, **kw) -> None:
        art = self.store.stage_artifact(run_id, name)
        if art is not None:
            log.info("stage %s: loaded from artifact", name)
            self._load(name, ctx, art)
            return
        self.store.stage_start(run_id, name)
        t0 = time.time()
        try:
            artifact = fn(run_id, ctx, **kw)
        except Abort as a:
            self.store.stage_done(run_id, name, {"abort": a.status, "msg": str(a)}, status=a.status, error=str(a))
            raise
        except Exception as e:  # noqa: BLE001
            self.store.stage_done(run_id, name, None, status="error", error=f"{type(e).__name__}: {e}")
            raise
        if isinstance(artifact, dict):
            artifact.setdefault("_seconds", round(time.time() - t0, 1))
        self.store.stage_done(run_id, name, artifact)

    def _load(self, name: str, ctx: Ctx, art: Dict[str, Any]) -> None:
        if name == "ingest":
            ctx.bars = pd.read_parquet(art["snapshot_path"])
            ctx.events = art["events"]
            ctx.documents = self.store.documents("2000", art.get("cutoff") or "9999", symbols=self.cfg.universe.tickers) if art.get("n_documents") else []
            ctx.doc_stats = art.get("doc_stats", {})
            ctx.cutoff = art.get("cutoff") or ctx.cutoff
        elif name == "validate":
            ctx.validation, ctx.ok_symbols = art, art["ok_symbols"]
        elif name == "account":
            ctx.account = art
        elif name == "features":
            ctx.scored = pd.DataFrame(art["table"]).set_index("symbol") if art.get("table") else None
            ctx.regime, ctx.macro, ctx.fundamentals = art["regime"], art.get("macro", {}), art.get("fundamentals", {})
            if ctx.scored is not None and not ctx.bars.empty:
                ctx.feature_panel = F.compute_feature_panel(F.Panel.from_long(ctx.bars[ctx.bars["symbol"].isin(ctx.ok_symbols)]),
                                                            self.cfg.features, self.cfg.universe.benchmark, self._sector_etf_map())
        elif name == "ai":
            ctx.ai_info = art
            ctx.ai_scores = {s: S.AIScore(**v) for s, v in art.get("scores", {}).items()}
        elif name == "v2_research":
            ctx.v2 = art
            ctx.v2_scores = {s: S.AIScore(**v) for s, v in art.get("scores", {}).items()}
        elif name == "propose":
            ctx.proposal = art
            ctx.arms = art.get("arms", {})
            if art.get("table"):
                ctx.scored = pd.DataFrame(art["table"]).set_index("symbol")
        elif name == "risk":
            ctx.risk = RK.RiskResult(approved=art["approved"], intents=[RK.OrderIntent(**i) for i in art["intents"]],
                                     decisions=[RK.RiskDecision(**d) for d in art["decisions"]], global_flags=art["global_flags"],
                                     block_new_exposure=art["block_new_exposure"], enter_safe_mode=art.get("enter_safe_mode"),
                                     stats=art.get("stats", {}), held_for_review=[RK.OrderIntent(**i) for i in art.get("held_for_review", [])])
        elif name == "execute":
            ctx.execution = art

    def _sector_etf_map(self) -> Dict[str, str]:
        from .events.entities import SECTOR_ETF
        return {s: SECTOR_ETF.get(sec, self.cfg.universe.benchmark) for s, sec in self.cfg.universe.symbols.items()}

    def _default_as_of(self) -> str:
        return self.calendar().last_completed_session()

    # ------------------------------------------------------------------ ingest
    def st_ingest(self, run_id: str, ctx: Ctx, replay_of: Optional[str] = None, research: bool = False) -> Dict[str, Any]:
        u = self.cfg.universe
        snap_dir = self.cfg.dir("data", "snapshots")
        if replay_of:
            orig = self.store.stage_artifact(replay_of, "ingest")
            if not orig:
                raise Abort("replay_missing_inputs", "original run has no ingest artifact")
            self._load("ingest", ctx, orig)
            art = dict(orig); art["replayed_from"] = replay_of
            return art
        as_of = date.fromisoformat(ctx.as_of)
        start = as_of - timedelta(days=self.cfg.data.lookback_days)
        md = self.market_data()
        bars = md.fetch_bars(u.tickers, start, as_of)
        if bars.empty:
            raise Abort("aborted_no_data", f"{md.name} returned no bars")
        bars = bars[bars["date"] <= ctx.as_of].reset_index(drop=True)
        latest = max(bars["date"])
        if latest != ctx.as_of:
            gap = (as_of - date.fromisoformat(latest)).days
            if gap > self.cfg.data.max_staleness_days:
                raise Abort("aborted_stale_data", f"latest bar {latest} is {gap}d older than {ctx.as_of} (max {self.cfg.data.max_staleness_days}d)")
            log.info("no bar for %s; using latest completed session %s as decision date", ctx.as_of, latest)
            ctx.as_of = latest
            self.store.x("UPDATE runs SET as_of=? WHERE run_id=?", (latest, run_id))
            if not research and self.store.q1("SELECT 1 FROM runs WHERE mode=? AND as_of=? AND status='ok' AND run_id<>?", (self.mode, latest, run_id)):
                raise Abort("skipped_already_completed", f"a run for session {latest} already completed")
        self.store.upsert_bars(bars.to_dict("records"), md.name)
        path = snap_dir / f"{run_id}.parquet"
        bars.to_parquet(path, index=False)
        ctx.bars = bars
        # ---- documents (V2) and V1 events derived from them
        since = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=self.cfg.data.events_lookback_days)
        rows: List[Dict[str, Any]] = []
        doc_stats: Dict[str, Any] = {}
        if self.cfg.v2.enabled:
            doc_stats = self._collect_documents(run_id, ctx, since, full=research)
            ctx.documents = self.store.documents(since.isoformat(), ctx.cutoff, symbols=u.tickers)
            rows = self._v1_events_from_docs(ctx.documents)
        if not rows and (self._es is not None or not self.cfg.v2.enabled):
            es = self.event_source()
            try:
                evs = es.fetch_events(u.tickers, since, self.cfg.data.events_max_per_symbol)
            except Exception as e:  # noqa: BLE001
                log.warning("event fetch failed (%s); continuing without events", e)
                evs = []
            rows = [e.to_row() for e in evs]
            if self.cfg.v2.enabled and evs:      # tests/file sources: convert events to documents for arm C
                docs = [SourceDocument(doc_id=f"ev_{e.event_id}", source="file", doc_type="news", symbols=[e.symbol], primary_symbol=e.symbol,
                                       title=e.title, summary=e.summary, published_at=e.published_at, retrieved_at=ctx.cutoff,
                                       available_at=e.published_at, source_quality=0.6, license_flags="test") for e in evs]
                doc_stats = self.docs.ingest(docs, run_id, set(u.tickers))
                ctx.documents = self.store.documents(since.isoformat(), ctx.cutoff, symbols=u.tickers)
        self.store.upsert_events(run_id, rows)
        ctx.events, ctx.doc_stats = rows, doc_stats
        return {"source": md.name, "n_bars": int(len(bars)), "symbols": sorted(bars["symbol"].unique().tolist()), "latest_date": latest,
                "bars_hash": bars_hash(bars), "snapshot_path": str(path), "events_source": "documents" if self.cfg.v2.enabled else self.event_source().name,
                "n_events": len(rows), "events": rows, "n_documents": len(ctx.documents), "doc_stats": doc_stats, "cutoff": ctx.cutoff,
                "fetched_at": utcnow()}

    def _collect_documents(self, run_id: str, ctx: Ctx, since: datetime, full: bool) -> Dict[str, Any]:
        """Collect news (primary + fallback), SEC filings, FRED, fundamentals. Failures degrade, never abort."""
        u = self.cfg.universe
        uni = set(u.tickers)
        stats: Dict[str, Any] = {"sources": {}, "degraded": []}
        srcs = self.doc_sources()
        got_primary = False
        for src in srcs:
            if got_primary and getattr(src, "name", "") == "yahoo":
                continue    # fallback only when the primary failed
            try:
                docs = src.fetch_documents(u.tickers, since, self.cfg.data.events_max_per_symbol * (3 if full else 1))
                st = self.docs.ingest(docs, run_id, uni)
                stats["sources"][src.name] = {k: v for k, v in st.items() if k != "ids"}
                if docs:
                    got_primary = got_primary or src.name != "yahoo"
            except Exception as e:  # noqa: BLE001
                log.warning("document source %s failed: %s", getattr(src, "name", src), e)
                stats["sources"][getattr(src, "name", "src")] = {"error": str(e)[:200]}
                stats["degraded"].append(f"{getattr(src, 'name', 'src')}_failed")
        if srcs and not got_primary:
            ctx.attention.append("primary_source_failed")
        ed = self.edgar()
        if ed is not None and full:
            try:
                reg = self.registry()
                n = 0
                sec_since = date.fromisoformat(ctx.as_of) - timedelta(days=self.cfg.v2.sources.sec_lookback_days)
                for sym in u.tickers:
                    cik = reg.cik(sym)
                    if not cik:
                        continue
                    docs = ed.recent_filings(sym, cik, self.cfg.v2.sources.sec_forms, sec_since)
                    st = self.docs.ingest(docs, run_id, uni)
                    n += st["new"]
                stats["sources"]["sec"] = {"new": n}
            except Exception as e:  # noqa: BLE001
                log.warning("SEC collection failed: %s", e)
                stats["degraded"].append("sec_failed")
        if self.cfg.v2.sources.fred_enabled and full:
            last = self.store.kv_get("fred:last_fetch")
            if last != ctx.run_date:
                try:
                    from .data.fred import Fred
                    fr = Fred()
                    n = sum(self.store.upsert_macro(fr.observations(s, self.cfg.v2.sources.fred_lookback_days)) for s in self.cfg.v2.sources.fred_series)
                    self.store.kv_set("fred:last_fetch", ctx.run_date)
                    stats["sources"]["fred"] = {"rows": n}
                except Exception as e:  # noqa: BLE001
                    stats["degraded"].append("fred_failed")
                    log.warning("FRED failed: %s", e)
        if self.cfg.v2.sources.fundamentals_enabled and ed is not None and full:
            try:
                reg = self.registry()
                n, refreshed = 0, 0
                for sym in u.tickers:
                    cik = reg.cik(sym)
                    if not cik:
                        continue
                    key = f"fund:last:{sym}"
                    last = self.store.kv_get(key)
                    if last and (date.fromisoformat(ctx.run_date) - date.fromisoformat(last)).days < 7:
                        continue
                    n += self.store.upsert_fundamentals(ed.fundamentals(sym, cik))
                    self.store.kv_set(key, ctx.run_date)
                    refreshed += 1
                stats["sources"]["fundamentals"] = {"rows": n, "symbols_refreshed": refreshed}
            except Exception as e:  # noqa: BLE001
                stats["degraded"].append("fundamentals_failed")
                log.warning("fundamentals failed: %s", e)
        return stats

    @staticmethod
    def _v1_events_from_docs(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Arm B sees the same documents as arm C, in V1's headline+summary form."""
        rows = []
        for d in docs:
            if d["doc_type"] != "news":
                continue
            for sym in d["symbols"][:3]:
                rows.append({"event_id": f"{d['doc_id']}_{sym}", "symbol": sym, "published_at": d["published_at"], "source": d["source"],
                             "title": d.get("title", ""), "summary": (d.get("summary") or "")[:1200], "url": d.get("url", ""),
                             "content_hash": d["content_hash"]})
        return rows

    # ------------------------------------------------------------------ validate / account
    def st_validate(self, run_id: str, ctx: Ctx) -> Dict[str, Any]:
        f = self.cfg.features
        min_rows = max(f.mom_long + f.skip_last_day + 5, f.trend_ma + 5)
        rep = validate_bars(ctx.bars, self.cfg.universe.tickers, self.cfg.universe.benchmark, date.fromisoformat(ctx.as_of), min_rows,
                            self.cfg.data.max_staleness_days)
        ctx.validation, ctx.ok_symbols = rep.to_dict(), rep.ok_symbols
        if rep.stale:
            raise Abort("aborted_stale_data", f"latest bar {rep.latest_date} older than {self.cfg.data.max_staleness_days}d")
        if not rep.benchmark_ok:
            raise Abort("aborted_benchmark_invalid", f"benchmark rejected: {rep.rejected.get(self.cfg.universe.benchmark)}")
        if not rep.ok:
            raise Abort("aborted_validation", "no valid symbols")
        return rep.to_dict()

    def _broker_snapshot(self, run_id: str, br: Broker, acct) -> Dict[str, Any]:
        try:
            oo = [{"symbol": o.symbol, "side": o.side, "qty": o.qty, "limit": o.limit_price, "status": o.status, "cid": o.client_order_id}
                  for o in br.open_orders()]
        except Exception:  # noqa: BLE001
            oo = []
        snap = {"source_time": utcnow(), "equity": acct.equity, "cash": acct.cash, "buying_power": acct.buying_power,
                "multiplier": acct.raw.get("multiplier", "1"), "positions": {s: {"qty": p.qty, "price": p.current_price} for s, p in acct.positions.items()},
                "open_orders": oo, "market": br.clock()}
        self.store.save_broker_snapshot(run_id, self.mode, snap)
        return snap

    def st_account(self, run_id: str, ctx: Ctx, replay_of: Optional[str] = None) -> Dict[str, Any]:
        if replay_of:
            orig = self.store.stage_artifact(replay_of, "account")
            if not orig:
                raise Abort("replay_missing_inputs", "original run has no account artifact")
            ctx.account = dict(orig, replayed_from=replay_of)
            return ctx.account
        day = ctx.bars[ctx.bars["date"] == ctx.as_of].set_index("symbol")
        closes, opens = day["close"].to_dict(), day["open"].to_dict()
        br = self.broker()
        recon: Dict[str, Any] = {}
        if isinstance(br, SimBroker):
            if br.s.get("date") != ctx.as_of:
                fills = br.settle(ctx.as_of, opens, closes)
                recon = {"sim_fills": [f.client_order_id for f in fills]}
            else:
                br.mark(closes, ctx.as_of)
        if self.cfg.submits_orders:
            try:
                recon.update(R.reconcile(self.store, br, self.cfg, run_id))
            except BrokerError as e:
                raise Abort("aborted_broker_unavailable", f"reconcile failed: {e}")
            if not recon.get("ok", True):
                ctx.attention.append("reconcile_mismatch")
                self.notifier().event("critical", "reconcile", f"broker/local mismatch: {recon.get('mismatches')} {recon.get('ambiguous')}", run_id,
                                      remediation="review positions; `sea-lion accept-broker-state` then `safe-mode --clear`")
        try:
            acct = br.account()
        except Exception as e:  # noqa: BLE001
            raise Abort("aborted_broker_unavailable", f"account fetch failed: {e}")
        snap = self._broker_snapshot(run_id, br, acct)
        mult = float(str(acct.raw.get("multiplier", "1")) or 1)
        if self.mode == "live" and self.cfg.v2.portfolio_risk.require_multiplier_one_live and mult > 1:
            ctx.attention.append("margin_account")
            self.notifier().event("critical", "broker", f"live account multiplier {mult} > 1 (margin enabled)", run_id)
        prev = self.store.latest_equity(self.mode)
        sod = acct.last_equity if acct.last_equity else (prev["equity"] if prev and prev["date"] < ctx.as_of else acct.equity)
        hwm = max(float(self.store.kv_get(f"hwm:{self.mode}", 0.0) or 0.0), acct.equity)
        self.store.kv_set(f"hwm:{self.mode}", hwm)
        positions = {s: {"qty": p.qty, "avg_price": p.avg_price, "price": p.current_price, "value": p.market_value} for s, p in acct.positions.items()}
        bench_close = closes.get(self.cfg.universe.benchmark)
        self.store.save_equity(ctx.as_of, self.mode, acct.equity, acct.cash, positions, hwm, sod, bench_close, br.name)
        safe = self.store.safe_mode_active()
        pending: Dict[str, float] = {}
        for o in snap["open_orders"]:
            if o.get("qty") and o.get("limit"):
                pending[o["symbol"]] = pending.get(o["symbol"], 0.0) + (1 if o["side"] == "buy" else -1) * float(o["qty"]) * float(o["limit"])
        ctx.account = {"broker": br.name, "equity": acct.equity, "cash": acct.cash, "buying_power": acct.buying_power, "positions": positions,
                       "start_of_day_equity": sod, "high_water_mark": hwm, "safe_mode": bool(safe), "safe_mode_reason": safe["reason"] if safe else None,
                       "reconcile": recon, "prices": closes, "benchmark_close": bench_close, "broker_raw": acct.raw, "clock": snap["market"],
                       "multiplier": mult, "pending_orders": pending, "open_orders": snap["open_orders"]}
        return ctx.account

    # ------------------------------------------------------------------ features
    def st_features(self, run_id: str, ctx: Ctx) -> Dict[str, Any]:
        bars = ctx.bars[ctx.bars["symbol"].isin(ctx.ok_symbols)]
        fp = F.compute_feature_panel(F.Panel.from_long(bars), self.cfg.features, self.cfg.universe.benchmark, self._sector_etf_map())
        ctx.feature_panel = fp
        feat = F.features_at(fp, ctx.as_of, ctx.ok_symbols)
        scored = S.quant_scores(feat, self.cfg.features, self.cfg.strategy)
        regime = F.market_regime(feat, self.cfg.universe.benchmark, self.cfg.strategy.regime)
        macro, fund = {}, {}
        if self.cfg.v2.enabled:
            try:
                macro = F.macro_features(self.store, ctx.as_of, ctx.cutoff)
                fund = F.fundamental_features(self.store, ctx.ok_symbols, ctx.as_of) if self.cfg.v2.sources.fundamentals_enabled else {}
                if macro.get("vix") and macro["vix"] >= 30 and regime["state"] == "bull":
                    regime = dict(regime, state="neutral", multiplier=self.cfg.strategy.regime.neutral, note="vix>=30 downgrade")
                self.store.save_feature_snapshots(ctx.as_of, self.cfg.v2.feature_version, F.snapshot_rows(scored), run_id)
            except Exception as e:  # noqa: BLE001
                log.warning("v2 features degraded: %s", e)
        ctx.scored, ctx.regime, ctx.macro, ctx.fundamentals = scored, regime, macro, fund
        return {"regime": regime, "n_eligible": int(scored["eligible"].sum()), "table": scored.reset_index().to_dict("records"),
                "macro": macro, "fundamentals": fund, "feature_version": self.cfg.v2.feature_version}

    # ------------------------------------------------------------------ arm B: V1 headline AI (unchanged contract)
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
        cached = self._research_artifact(ctx.as_of, "ai")
        if cached and not replay and cached.get("available"):
            ctx.ai_info = dict(cached, reused_from_research=True, research_seconds=cached.get("_seconds"), _seconds=0.0)
            ctx.ai_scores = {s: S.AIScore(**v) for s, v in cached.get("scores", {}).items()}
            return ctx.ai_info
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
                        "trend_vs_ma50": _r(row.get("trend")), "realized_vol_20d": _r(row.get("vol")), "volume_ratio": _r(row.get("volume_ratio")),
                        "quant_score": _r(row.get("quant_score")), "market_regime": ctx.regime.get("state")}
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

    def _research_artifact(self, as_of: str, stage: str) -> Optional[Dict[str, Any]]:
        rr = self.store.research_run(as_of)
        if not rr or rr["status"] != "ok":
            return None
        return self.store.stage_artifact(rr["run_id"], stage)

    # ------------------------------------------------------------------ arm C: V2 verified-event research
    def st_v2_research(self, run_id: str, ctx: Ctx, replay: bool = False, full: bool = False) -> Dict[str, Any]:
        rc = self.cfg.v2.research
        art: Dict[str, Any] = {"enabled": self.cfg.v2.enabled and rc.enabled, "scores": {}, "forecasts": {}, "candidates": [],
                               "degraded": [], "deadline_hit": False, "reused_from_research": None, "delta": {}, "events": [], "n_events": 0}
        ctx.v2 = art
        if not art["enabled"]:
            art["skipped_reason"] = "disabled"
            return art
        if not ctx.documents:
            art["skipped_reason"] = "no_documents"
            return art
        month_start = ctx.as_of[:7] + "-01"
        router = ModelRouter(self.cfg.ai, self.store, run_id, ctx.as_of, month_start, ctx.ok_symbols, replay=replay)
        snapshots = self._snapshots(ctx)
        quant_rank = list(ctx.scored[ctx.scored["eligible"].astype(bool)].sort_values("quant_score", ascending=False).index)
        cached = None if (full or replay) else self._research_artifact(ctx.as_of, "v2_research")
        # after-close (full): per-stage budgets; morning: one hard budget = what is left of the decision deadline
        deadline = None if full else max(60, rc.morning_deadline_sec - int(time.time() - ctx.t_start) - 60)
        rp = ResearchPipeline(self.cfg, self.store, router, self.docs, self.registry(), run_id, ctx.as_of, ctx.cutoff, ctx.ok_symbols, deadline)
        if cached and cached.get("candidates") is not None:
            # morning path: reuse; process only the overnight delta
            watermark = (self.store.research_run(ctx.as_of) or {}).get("doc_watermark_json")
            import json as _j
            wm = _j.loads(watermark or "{}").get("docs_until", "")
            seen = {x for x in cached.get("doc_ids", [])}
            delta_all = [d for d in ctx.documents if d["available_at"] > wm and d["doc_id"] not in seen]
            # the morning only needs material updates about names that could trade today: current
            # candidates and holdings, filings first, best sources first, hard-capped (design §5.2, §8.3)
            focus = set(cached.get("candidates", [])) | {s for s, p in (ctx.account.get("positions") or {}).items() if p.get("value", 0) >= self.cfg.risk.min_order_notional}
            delta_docs = sorted([d for d in delta_all if (set(d["symbols"]) & focus) and (d["doc_type"] == "filing" or (d.get("source_quality") or 0) >= 0.6)],
                                key=lambda d: (0 if d["doc_type"] == "filing" else 1, -(d.get("source_quality") or 0)))[: rc.morning_delta_max_docs]
            art = dict(cached, reused_from_research=self.store.research_run(ctx.as_of)["run_id"],
                       research_degraded=list(cached.get("degraded", [])), degraded=[], deadline_hit=False,
                       delta={"n_docs": len(delta_docs), "n_new_docs_total": len(delta_all), "focus_symbols": sorted(focus)})
            if delta_docs and not replay:
                meta = rp.extract(delta_docs)
                new_events = rp.build_events(delta_docs, meta, snapshots)
                hot = sorted({e["primary_symbol"] for e in new_events if e.get("actionable")})
                art["delta"].update({"n_new_events": len(new_events), "new_actionable_symbols": hot, "extraction": rp.res.extraction})
                rerun = [s for s in hot if s in cached.get("candidates", [])][: max(1, rc.main_candidates // 2)]
                if rerun:
                    events_all = self.store.events((datetime.fromisoformat(ctx.cutoff.replace("Z", "+00:00")) - timedelta(days=self.cfg.data.events_lookback_days)).isoformat(), ctx.cutoff, symbols=rerun)
                    packets = rp.run_candidates(rerun, events_all, snapshots, ctx.regime, ctx.macro)
                    art["forecasts"].update(packets)
                    art["delta"]["reran"] = rerun
                    art["degraded"] = list(art.get("degraded", [])) + rp.res.degraded
                    art["deadline_hit"] = art.get("deadline_hit") or rp.res.deadline_hit
            art["scores"] = self._v2_scores(ctx, art["forecasts"], run_id)
            art["events"] = self.store.events((datetime.fromisoformat(ctx.cutoff.replace("Z", "+00:00")) - timedelta(days=self.cfg.data.events_lookback_days)).isoformat(), ctx.cutoff, symbols=ctx.ok_symbols)
            art["n_events"] = len(art["events"])
        else:
            meta = rp.extract(ctx.documents)
            events = rp.build_events(ctx.documents, meta, snapshots)
            cands = rp.choose_candidates(events, quant_rank)
            packets = rp.run_candidates(cands, events, snapshots, ctx.regime, ctx.macro)
            r = rp.res.to_dict()
            art.update({"extraction": r["extraction"], "events": events, "n_events": len(events), "candidates": cands,
                        "candidate_reasons": r["candidate_reasons"], "forecasts": packets, "degraded": r["degraded"],
                        "deadline_hit": r["deadline_hit"], "timings": r["timings"], "doc_ids": [d["doc_id"] for d in ctx.documents],
                        "events_routed": {c: sum(1 for e in events if e.get("routing_class") == c) for c in {e.get("routing_class") for e in events}},
                        "source_coverage": ctx.doc_stats})
            art["scores"] = self._v2_scores(ctx, packets, run_id)
        art["router"] = router.state.to_dict()
        if router.state.budget_status == "hard_stop" and "budget_exhausted" not in art["degraded"]:
            art["degraded"] = list(art["degraded"]) + ["budget_exhausted"]
        art["available"] = not router.state.provider_unavailable and router.state.budget_status != "hard_stop"
        if art["deadline_hit"] and not full:
            ctx.notes.append("morning_research_delta_deadline")     # degradation, reported; the execute-stage deadline is the alert
        ctx.v2 = art
        ctx.v2_scores = {s: S.AIScore(**v) for s, v in art["scores"].items()}
        return art

    def _snapshots(self, ctx: Ctx) -> Dict[str, Dict[str, Any]]:
        out = {}
        for sym, row in ctx.scored.iterrows():
            snap = {"as_of": ctx.as_of, "market_regime": ctx.regime.get("state")}
            for c in ("mom_short", "mom_long", "mom_5", "mom_120", "trend", "trend_200", "vol", "downside_vol", "volume_ratio", "rs_spy_20",
                      "rs_sector_20", "resid_5", "beta_60", "corr_spy_60", "drawdown_60", "gap_risk", "quant_score"):
                if c in row.index:
                    snap[c] = _r(row.get(c))
            f = (ctx.fundamentals or {}).get(sym) or {}
            snap.update({k: _r(v) for k, v in f.items()})
            out[sym] = snap
        return out

    def _v2_scores(self, ctx: Ctx, packets: Dict[str, Dict[str, Any]], run_id: str) -> Dict[str, Dict[str, Any]]:
        """Forecast packet -> calibrated overlay in [-1,1] with a confidence surrogate that clears the V1
        floor only when the deterministic gates pass (so strategy.ensemble can be reused unchanged)."""
        rc = self.cfg.v2.research
        scores: Dict[str, Dict[str, Any]] = {}
        for sym, p in packets.items():
            synth = p.get("synth")
            abstain = bool(p.get("abstain", True)) or synth is None
            horizons = (synth or {}).get("horizons") or {}
            cal, mode = FC.calibrate_forecast(self.store, horizons, rc.shrink_to_half) if horizons else ({}, "none")
            eq = float(p.get("evidence_quality") or 0.0)
            ov, info = FC.overlay_score(cal, eq, float(p.get("freshness") or 0.0), abstain, rc.evidence_quality_floor) if cal else (0.0, {"reason": "no_forecast"})
            usable = not abstain and info.get("reason") == "ok"
            scores[sym] = {"score": round(ov, 6), "confidence": 1.0 if usable else 0.0, "risk_flags": (synth or {}).get("risk_flags", []),
                           "evidence_ids": (synth or {}).get("evidence_ids", []), "horizon_days": 10}
            p["calibrated"], p["calibration_mode"], p["overlay"], p["overlay_info"] = cal, mode, round(ov, 6), info
            if not run_id.endswith("-replay"):
                px = ctx.account.get("prices", {}).get(sym) if ctx.account else None
                bpx = ctx.account.get("benchmark_close") if ctx.account else None
                if px is None and ctx.scored is not None and sym in ctx.scored.index:
                    px = _r(ctx.scored.loc[sym, "close"])
                    bpx = _r(ctx.scored.loc[self.cfg.universe.benchmark, "close"]) if self.cfg.universe.benchmark in ctx.scored.index else None
                FC.freeze(self.store, run_id, "C", ctx.as_of, ctx.cutoff, sym, horizons, (synth or {}), cal, eq,
                          float(p.get("disagreement") or 0.0), abstain, (synth or {}).get("evidence_ids", []), (synth or {}).get("risk_flags", []),
                          round(ov, 6), px, bpx, self.cfg.v2.forecast_version)
        return scores

    # ------------------------------------------------------------------ propose: arms A/B/C
    def st_propose(self, run_id: str, ctx: Ctx) -> Dict[str, Any]:
        holdings = [s for s, p in (ctx.account.get("positions") or {}).items() if p.get("value", 0) >= self.cfg.risk.min_order_notional]
        arms_out: Dict[str, Dict[str, Any]] = {}
        if self.cfg.v2.enabled and self.cfg.v2.arms.enabled:
            for arm in ARMS.ARMS:
                arms_out[arm] = ARMS.build_arm(arm, ctx.scored, ctx.ai_scores, ctx.v2_scores, ctx.regime, self.cfg, holdings)
            orders_arm = self.cfg.v2.arms.orders_arm
        else:
            arms_out["B"] = ARMS.build_arm("B", ctx.scored, ctx.ai_scores, {}, ctx.regime, self.cfg, holdings)
            arms_out["A"] = ARMS.build_arm("A", ctx.scored, {}, {}, ctx.regime, self.cfg, holdings)
            orders_arm = "B"
        chosen = arms_out[orders_arm]
        ai_for_orders = {"A": {}, "B": ctx.ai_scores, "C": ctx.v2_scores}[orders_arm]
        scored = S.ensemble(ctx.scored, ai_for_orders, self.cfg.strategy, self.cfg.risk)
        ctx.scored = scored
        cands, weights = chosen["candidates"], chosen["target_weights"]
        qcands, qweights = arms_out["A"]["candidates"], arms_out["A"]["target_weights"]
        rank = {s: i + 1 for i, s in enumerate(cands)}
        decisions = []
        for sym, row in scored.iterrows():
            a = ai_for_orders.get(sym)
            decisions.append({"symbol": sym, "as_of": ctx.as_of, "horizon_days": a.horizon_days if a else 5, "quant_score": _r(row["quant_score"]),
                              "ai_score": _r(row["ai_score"]), "ensemble_score": _r(row["ensemble_score"]), "confidence": _r(row["ai_confidence"]),
                              "risk_flags": a.risk_flags if a else [], "evidence_ids": a.evidence_ids if a else [],
                              "prompt_version": self.cfg.v2.prompt_version if orders_arm == "C" else self.cfg.ai.prompt_version, "rank": rank.get(sym),
                              "proposed_weight": weights.get(sym, 0.0), "eligible": bool(row["eligible"]),
                              "features": {k: _r(row.get(k)) for k in ["mom_short", "mom_long", "trend", "vol", "volume_ratio"]}})
        self.store.save_decisions(run_id, decisions)
        ctx.arms = arms_out
        ctx.proposal = {"orders_arm": orders_arm, "weights": weights, "quant_only_weights": qweights, "candidates": cands,
                        "quant_only_candidates": qcands, "regime": ctx.regime,
                        "ai_contribution": {s: _r(scored.loc[s, "ai_score"] * self.cfg.strategy.ai_weight_cap) for s in cands},
                        "arms": {a: {k: v for k, v in ar.items() if k in ("name", "candidates", "target_weights", "hysteresis_candidates",
                                                                           "hysteresis_weights", "hysteresis_applied", "hysteresis_why", "ai_contribution")}
                                 for a, ar in arms_out.items()},
                        "table": scored.reset_index().to_dict("records")}
        return ctx.proposal

    # ------------------------------------------------------------------ risk (orders arm + shadow arms)
    def _account_state(self, ctx: Ctx, positions: Dict[str, float], equity: float, cash: float, sod: float, hwm: float,
                       safe_mode: bool, pending: Dict[str, float]) -> RK.AccountState:
        betas, clusters, gap = {}, {}, {}
        if self.cfg.v2.enabled and ctx.scored is not None:
            if "beta_60" in ctx.scored.columns:
                betas = {s: (None if pd.isna(v) else float(v)) for s, v in ctx.scored["beta_60"].items()}
            if "gap_risk" in ctx.scored.columns:
                gap = {s: (0.01 if pd.isna(v) else float(v)) for s, v in ctx.scored["gap_risk"].items()}
            if ctx.feature_panel is not None:
                try:
                    clusters = PF.correlation_clusters(ctx.feature_panel["adj_close"], ctx.as_of, self.cfg.v2.portfolio_risk.correlation_window,
                                                       self.cfg.v2.portfolio_risk.correlation_threshold)
                except Exception as e:  # noqa: BLE001
                    log.warning("cluster computation failed: %s", e)
        binary = PF.binary_event_symbols(ctx.v2.get("events") or []) if ctx.v2 else set()
        health_ok = not any(a in ("error", "margin_account") for a in ctx.attention)
        st = RK.AccountState(equity=equity, cash=cash, positions=positions, prices=ctx.account["prices"], start_of_day_equity=sod,
                             high_water_mark=hwm, data_stale=bool(ctx.validation.get("stale")), safe_mode=safe_mode, market_open_next_session=True,
                             pending_orders=pending, betas=betas, clusters=clusters, binary_event_symbols=binary, health_ok=health_ok,
                             multiplier=float(ctx.account.get("multiplier") or 1.0))
        st._gap = gap  # type: ignore[attr-defined]
        return st

    def st_risk(self, run_id: str, ctx: Ctx) -> Dict[str, Any]:
        a = ctx.account
        pcfg = self.cfg.v2.portfolio_risk if self.cfg.v2.enabled else None
        acct = self._account_state(ctx, {s: p["qty"] for s, p in a["positions"].items()}, a["equity"], a["cash"], a["start_of_day_equity"],
                                   a["high_water_mark"], bool(a.get("safe_mode")), a.get("pending_orders", {}))
        res = RK.evaluate(ctx.proposal["weights"], acct, self.cfg.universe.symbols, self.cfg.risk, rank_order=ctx.proposal["candidates"],
                          pcfg=pcfg, is_live=self.mode == "live")
        if pcfg:
            res.stats["scenarios"] = PF.scenario_checks(res.approved, acct.betas, self.cfg.universe.symbols, acct.clusters, getattr(acct, "_gap", {}), pcfg)
        if res.enter_safe_mode:
            self.store.enter_safe_mode(res.enter_safe_mode, run_id)
            ctx.attention.append("safe_mode")
            self.notifier().event("critical", "risk", f"safe mode: {res.enter_safe_mode}", run_id, remediation="review; `sea-lion safe-mode --clear`")
        orders_arm = ctx.proposal.get("orders_arm", "B")
        ai_info = ctx.ai_info if orders_arm == "B" else (ctx.v2 or {})
        ai_needed = self.cfg.ai.enabled and orders_arm != "A" and ai_info.get("skipped_reason") not in ("no_new_events", "disabled", "no_documents")
        if ai_needed and not ai_info.get("available", False):
            res.global_flags.append("ai_unavailable_quant_only")
            if self.mode in ("paper", "live"):
                ctx.attention.append("ai_unavailable_paper_live")
                self.notifier().event("warning", "ai", "model layer unavailable; decision is quant-only", run_id)
            if self.mode not in self.cfg.ai.quant_only_orders_allowed_modes:
                res.global_flags.append("ai_unavailable_orders_blocked")
                res.held_for_review.extend(res.intents)
                res.intents = []
        ctx.risk = res
        final = dict(res.approved)
        for d in res.decisions:
            final[d.symbol] = d.approved_weight
        with self.store.tx() as c:
            for s, w in final.items():
                c.execute("UPDATE decisions SET approved_weight=? WHERE run_id=? AND symbol=?", (w, run_id, s))
        # ---- shadow arms: same cutoff, own simulated book, own risk pass; divergence vs quant-only
        if self.cfg.v2.enabled and self.cfg.v2.arms.enabled and ctx.arms:
            self._shadow_arms(run_id, ctx, res)
        out = res.to_dict()
        out["orders_arm"] = orders_arm
        return out

    def _shadow_arms(self, run_id: str, ctx: Ctx, orders_res: RK.RiskResult) -> None:
        day = ctx.bars[ctx.bars["date"] == ctx.as_of].set_index("symbol")
        opens, closes = day["open"].to_dict(), day["close"].to_dict()
        orders_arm = ctx.proposal.get("orders_arm", "B")
        results: Dict[str, Dict[str, Any]] = {}
        intents_by_arm: Dict[str, List[RK.OrderIntent]] = {orders_arm: orders_res.intents}
        for arm in ARMS.ARMS:
            ar = ctx.arms.get(arm)
            if ar is None:
                continue
            payload = {"target_weights": ar["target_weights"], "candidates": ar["candidates"], "is_orders_arm": arm == orders_arm,
                       "assumptions": {"slippage_bps": self.cfg.v2.arms.shadow_slippage_bps, "fill": "next_open", "hysteresis": ar["hysteresis_applied"]}}
            if arm == orders_arm:
                payload.update({"approved": orders_res.approved, "intents": [i.__dict__ for i in orders_res.intents], "sim_equity": ctx.account["equity"],
                                "sim_cash": ctx.account["cash"], "sim_positions": {s: p["qty"] for s, p in ctx.account["positions"].items()}})
            else:
                sh = ARMS.ShadowArm(self.store, self.mode, arm, self.cfg)
                sh.settle(ctx.as_of, opens, closes)
                st = sh.account_state(ctx.account["prices"], f"hwm:{self.mode}:arm{arm}")
                st2 = self._account_state(ctx, st.positions, st.equity, st.cash, st.start_of_day_equity, st.high_water_mark, False, {})
                r = RK.evaluate(ar["target_weights"], st2, self.cfg.universe.symbols, self.cfg.risk, rank_order=ar["candidates"],
                                pcfg=self.cfg.v2.portfolio_risk, is_live=False)
                subs = sh.submit(run_id, r.intents, ctx.account["prices"])
                payload.update({"approved": r.approved, "intents": subs, **sh.snapshot()})
                intents_by_arm[arm] = r.intents
            self.store.save_arm(run_id, arm, ctx.as_of, payload)
            results[arm] = payload
        base = ctx.arms.get("A")
        if base:
            for arm in ("B", "C"):
                if arm in ctx.arms:
                    d = ARMS.divergence(base, ctx.arms[arm], intents_by_arm.get("A", []), intents_by_arm.get(arm, []))
                    self.store.save_divergence(run_id, arm, "A", ctx.as_of, d)
                    results[arm]["divergence_vs_A"] = d
        ctx.proposal["arm_results"] = {a: {k: v for k, v in p.items() if k in ("sim_equity", "sim_cash", "divergence_vs_A")} for a, p in results.items()}

    # ------------------------------------------------------------------ execute
    def st_execute(self, run_id: str, ctx: Ctx, replay: bool = False, dry_run: bool = False) -> Dict[str, Any]:
        res = ctx.risk
        base = {"mode": self.mode, "orders": "disabled", "would_submit": [i.__dict__ for i in res.intents],
                "held_for_review": [i.__dict__ for i in res.held_for_review]}
        if replay:
            return dict(base, mode="replay")
        if not self.cfg.submits_orders:
            return base
        if dry_run:
            return dict(base, mode=f"{self.mode}-dry-run")
        if self.mode == "live":
            self._live_gate()
        elapsed = time.time() - ctx.t_start
        if self.cfg.v2.enabled and elapsed > self.cfg.v2.research.morning_deadline_sec:
            buys = [i for i in res.intents if i.side == "buy"]
            res.held_for_review.extend(buys)
            res.intents = [i for i in res.intents if i.side != "buy"]
            res.global_flags.append(f"morning_deadline_exceeded_{int(elapsed)}s_buys_held")
            ctx.attention.append("deadline_exceeded")
            self.notifier().event("warning", "pipeline", f"decision took {int(elapsed)}s > deadline; new exposure withheld", run_id)
        if not res.intents:
            return {"mode": self.mode, "submitted": [], "skipped": [], "errors": [], "ambiguous": [], "duplicates_prevented": 0, "note": "no intents",
                    "held_for_review": base["held_for_review"]}
        br = self.broker()
        if isinstance(br, SimBroker):
            fresh, market_open_next = ctx.account["prices"], True
        else:
            if self.cfg.broker.trading_hours_only and not self.calendar().in_decision_window(self.cfg.v2.calendar.decision_window_local):
                res.held_for_review.extend(res.intents)
                res.global_flags.append("outside_decision_window")
                ctx.attention.append("decision_window_missed")
                self.notifier().event("warning", "execution", "outside the regular-hours decision window; orders withheld (never queued overnight)", run_id)
                return {"mode": self.mode, "submitted": [], "skipped": [{"why": "outside_decision_window"}], "errors": [], "ambiguous": [],
                        "duplicates_prevented": 0, "held_for_review": [i.__dict__ for i in res.held_for_review]}
            fresh = self._fresh_prices([i.symbol for i in res.intents]) or ctx.account["prices"]
            clock = ctx.account.get("clock") or {}
            market_open_next = bool(clock.get("is_open") or clock.get("next_open"))
        rep = execution.execute(res.intents, br, self.store, self.cfg, run_id, fresh, market_open_next)
        if rep.ambiguous:
            self.store.enter_safe_mode(f"ambiguous broker response for {[a['symbol'] for a in rep.ambiguous]}", run_id)
            ctx.attention.append("ambiguous_orders")
            self.notifier().event("critical", "execution", f"ambiguous broker responses: {rep.ambiguous}", run_id)
        ctx.execution = rep.to_dict()
        ctx.execution["held_for_review"] = base["held_for_review"]
        return ctx.execution

    def _fresh_prices(self, symbols: List[str]) -> Dict[str, float]:
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
        expected = f"LIVE-{datetime.now(ET).date().isoformat()}"     # the trading day, in Eastern time
        if os.environ.get("SEA_LION_LIVE_CONFIRM_TOKEN") != expected:
            raise Abort("live_blocked", f"manual confirmation token must equal {expected} (set daily)")
        paper_db = Path(self.cfg.runtime_dir) / "db" / "sea_lion_paper.db"
        n = Store(paper_db).completed_sessions("paper") if paper_db.exists() else 0
        if n < self.cfg.broker.paper_sessions_required_before_live:
            raise Abort("live_blocked", f"only {n} paper sessions completed; need {self.cfg.broker.paper_sessions_required_before_live}")

    # ------------------------------------------------------------------ reconcile-only / summary / outcomes / report
    def _reconcile_only(self, run_id: str, ctx: Ctx) -> None:
        """Design §4.11/§14: a skipped decision still reconciles the book and snapshots the account."""
        if not self.cfg.submits_orders:
            return
        try:
            br = self.broker()
            rec = R.reconcile(self.store, br, self.cfg, run_id)
            acct = br.account()
            self._broker_snapshot(run_id, br, acct)
            self.store.stage_start(run_id, "reconcile_only")
            self.store.stage_done(run_id, "reconcile_only", dict(rec, equity=acct.equity, cash=acct.cash))
            ctx.account = {"equity": acct.equity, "cash": acct.cash, "reconcile": rec, "positions": {s: {"qty": p.qty, "value": p.market_value} for s, p in acct.positions.items()}}
            if not rec.get("ok", True):
                ctx.attention.append("reconcile_mismatch")
                self.notifier().event("critical", "reconcile", f"mismatch during reconcile-only: {rec.get('mismatches')}", run_id)
        except Exception as e:  # noqa: BLE001
            log.error("reconcile-only failed: %s", e)
            ctx.attention.append("error")
            self.notifier().event("critical", "reconcile", f"reconcile-only failed: {e}", run_id)

    def _summary(self, run_id: str, ctx: Ctx, status: str, outcome: str, err: Optional[str]) -> Dict[str, Any]:
        ex = ctx.execution or {}
        rec = (ctx.account or {}).get("reconcile") or {}
        ai_avail = None
        if ctx.proposal.get("orders_arm") == "C":
            ai_avail = (ctx.v2 or {}).get("available")
        elif ctx.ai_info:
            ai_avail = ctx.ai_info.get("available")
        summ = {"run_date": ctx.run_date, "decision_as_of": ctx.as_of, "run_outcome": outcome, "status": status, "error": err,
                "safe_mode": bool(self.store.safe_mode_active()), "n_orders": len(ex.get("submitted", []) or []),
                "n_held_for_review": len((ctx.risk.held_for_review if ctx.risk else []) or []), "ai_available": ai_avail,
                "orders_arm": ctx.proposal.get("orders_arm"), "reconciliation_status": "ok" if rec.get("ok", True) else "mismatch",
                "attention": bool(set(ctx.attention) & ATTENTION_FLAGS), "attention_flags": sorted(set(ctx.attention)),
                "cutoff": ctx.cutoff, "elapsed_sec": round(time.time() - ctx.t_start), "code_version": code_version(),
                "config_hash": self.cfg.config_hash, "v2_degraded": (ctx.v2 or {}).get("degraded"),
                "research_degraded": (ctx.v2 or {}).get("research_degraded"),
                "divergence_C_vs_A": ((ctx.proposal.get("arm_results") or {}).get("C") or {}).get("divergence_vs_A", {}).get("weight_distance") if ctx.proposal else None}
        self.store.stage_start(run_id, "summary")
        self.store.stage_done(run_id, "summary", summ)
        return summ

    def _score_outcomes(self, ctx: Ctx) -> Optional[Dict[str, Any]]:
        if not self.cfg.v2.enabled or ctx.bars.empty:
            return None
        panel = F.Panel.from_long(ctx.bars)
        sessions = [d for d in panel.adj_close.index]
        return FC.score_outcomes(self.store, sessions, panel.adj_close, self.cfg.universe.benchmark, self.cfg.v2.research.horizons)

    def _report(self, run_id: str, run_date: str) -> Dict[str, str]:
        rep = REP.build_report(self.store, run_id, self.mode)
        as_of = rep["run"].get("as_of", "unknown")
        return REP.write_reports(rep, self.cfg.dir("reports", self.mode), as_of, run_id, self.cfg.reporting.write_html, self.cfg.reporting.write_json,
                                 run_date=run_date)

    def _compare_replay(self, orig_id: str, new_id: str) -> Dict[str, Any]:
        a = self.store.stage_artifact(orig_id, "propose") or {}
        b = self.store.stage_artifact(new_id, "propose") or {}
        wa, wb = a.get("weights", {}), b.get("weights", {})
        diffs = {s: (wa.get(s), wb.get(s)) for s in set(wa) | set(wb) if abs((wa.get(s) or 0) - (wb.get(s) or 0)) > 1e-6}
        return {"identical": not diffs and bool(a) and bool(b), "diffs": diffs, "orig_candidates": a.get("candidates"), "new_candidates": b.get("candidates")}


def _r(v: Any, nd: int = 6) -> Optional[float]:
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None
