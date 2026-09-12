"""SQLite persistence: immutable run records, inputs, model calls, decisions, orders, metrics.

One database per environment (research/paper/live are separate files). Every pipeline
stage writes its artifact here before the next stage starts, which is what makes a
run replayable and a crash resumable.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, as_of TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
  code_version TEXT, config_hash TEXT, strategy_version TEXT, error TEXT,
  replay_of TEXT
);
CREATE TABLE IF NOT EXISTS run_stages (
  run_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, input_hash TEXT,
  artifact_json TEXT, error TEXT,
  PRIMARY KEY (run_id, stage)
);
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL,
  source TEXT, fetched_at TEXT,
  PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, published_at TEXT NOT NULL,
  source TEXT, title TEXT, summary TEXT, url TEXT, content_hash TEXT,
  fetched_at TEXT, first_run_id TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
  run_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY (run_id, event_id)
);
CREATE TABLE IF NOT EXISTS ai_cache (
  input_hash TEXT PRIMARY KEY, tier TEXT, provider TEXT, model TEXT,
  prompt_version TEXT, schema_version TEXT, output_json TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS model_calls (
  call_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, tier TEXT, provider TEXT, model TEXT,
  prompt_version TEXT, schema_version TEXT, input_hash TEXT, cache_hit INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER, cost_usd REAL,
  valid INTEGER, error TEXT, output_json TEXT, raw_text TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  run_id TEXT NOT NULL, symbol TEXT NOT NULL, as_of TEXT, horizon_days INTEGER,
  quant_score REAL, ai_score REAL, ensemble_score REAL, confidence REAL,
  risk_flags_json TEXT, evidence_ids_json TEXT, prompt_version TEXT,
  rank INTEGER, proposed_weight REAL, approved_weight REAL, eligible INTEGER,
  features_json TEXT,
  PRIMARY KEY (run_id, symbol)
);
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, qty REAL, notional REAL, limit_price REAL, order_type TEXT,
  status TEXT NOT NULL, broker_order_id TEXT, submitted_at TEXT,
  filled_qty REAL DEFAULT 0, filled_avg_price REAL, fees REAL DEFAULT 0,
  updated_at TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
  date TEXT NOT NULL, mode TEXT NOT NULL, equity REAL, cash REAL,
  positions_json TEXT, high_water_mark REAL, start_of_day_equity REAL,
  benchmark_close REAL, source TEXT, created_at TEXT,
  PRIMARY KEY (date, mode)
);
CREATE TABLE IF NOT EXISTS cost_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, category TEXT NOT NULL,
  usd REAL NOT NULL, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  run_id TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS safe_mode (
  id INTEGER PRIMARY KEY AUTOINCREMENT, activated_at TEXT NOT NULL, reason TEXT NOT NULL,
  run_id TEXT, cleared_at TEXT, cleared_by TEXT
);
CREATE TABLE IF NOT EXISTS reconciliations (
  run_id TEXT PRIMARY KEY, ok INTEGER NOT NULL, mismatches_json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
-- ---------------------------------------------------------------- V2 (design §16)
CREATE TABLE IF NOT EXISTS source_documents (
  doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, provider_id TEXT, doc_type TEXT NOT NULL,
  symbols_json TEXT NOT NULL, primary_symbol TEXT, title TEXT, summary TEXT, content_ref TEXT,
  url TEXT, event_time TEXT, published_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
  available_at TEXT NOT NULL, effective_date TEXT, content_hash TEXT NOT NULL,
  source_quality REAL, license_flags TEXT, quarantined INTEGER DEFAULT 0, quarantine_reason TEXT,
  meta_json TEXT, first_run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_pub ON source_documents(published_at);
CREATE INDEX IF NOT EXISTS idx_docs_hash ON source_documents(content_hash);
CREATE TABLE IF NOT EXISTS document_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL, prior_hash TEXT, new_hash TEXT,
  revised_at TEXT, relationship TEXT
);
CREATE TABLE IF NOT EXISTS entity_mentions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL, symbol TEXT, cik TEXT, span TEXT,
  confidence REAL, method TEXT
);
CREATE TABLE IF NOT EXISTS atomic_claims (
  claim_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, symbol TEXT, claim TEXT NOT NULL,
  evidence_span TEXT, quantity TEXT, claim_time TEXT, certainty REAL, event_type TEXT,
  created_at TEXT, run_id TEXT
);
CREATE TABLE IF NOT EXISTS canonical_events (
  event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, primary_symbol TEXT NOT NULL,
  related_symbols_json TEXT, event_time TEXT, available_at TEXT NOT NULL, status TEXT NOT NULL,
  novelty REAL, source_quality REAL, contradiction_level REAL, importance REAL, sentiment REAL,
  fingerprint TEXT, title TEXT, document_ids_json TEXT, claim_ids_json TEXT,
  supersedes_event_id TEXT, created_at TEXT, updated_at TEXT, first_run_id TEXT, last_run_id TEXT,
  routing_class TEXT, actionable INTEGER DEFAULT 0, audit_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_sym ON canonical_events(primary_symbol, available_at);
CREATE TABLE IF NOT EXISTS event_claim_links (
  event_id TEXT NOT NULL, claim_id TEXT NOT NULL, role TEXT, PRIMARY KEY (event_id, claim_id)
);
CREATE TABLE IF NOT EXISTS feature_snapshots (
  as_of TEXT NOT NULL, symbol TEXT NOT NULL, feature_version TEXT NOT NULL,
  values_json TEXT NOT NULL, missing_json TEXT, run_id TEXT, PRIMARY KEY (as_of, symbol, feature_version)
);
CREATE TABLE IF NOT EXISTS forecasts (
  forecast_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, arm TEXT NOT NULL, symbol TEXT NOT NULL,
  as_of TEXT NOT NULL, decision_time TEXT NOT NULL, forecast_version TEXT, horizons_json TEXT NOT NULL,
  raw_json TEXT, calibrated_json TEXT, evidence_quality REAL, model_disagreement REAL,
  abstain INTEGER DEFAULT 0, evidence_ids_json TEXT, risk_flags_json TEXT, frozen_at TEXT NOT NULL,
  overlay_score REAL, base_price REAL, benchmark_base REAL
);
CREATE TABLE IF NOT EXISTS forecast_outcomes (
  forecast_id TEXT NOT NULL, horizon INTEGER NOT NULL, scored_at TEXT, matured_date TEXT,
  realized_return REAL, benchmark_return REAL, excess_return REAL, p_positive REAL, hit INTEGER,
  brier REAL, status TEXT, PRIMARY KEY (forecast_id, horizon)
);
CREATE TABLE IF NOT EXISTS calibration_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT, horizon INTEGER, version TEXT, trained_at TEXT, n_samples INTEGER,
  params_json TEXT, metrics_json TEXT, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS health_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, severity TEXT NOT NULL, component TEXT NOT NULL,
  reason TEXT NOT NULL, run_id TEXT, remediation TEXT, acknowledged_at TEXT, acknowledged_by TEXT
);
CREATE TABLE IF NOT EXISTS notification_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, health_event_id INTEGER, channel TEXT, destination_hash TEXT,
  status TEXT, attempt INTEGER, error TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS broker_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, mode TEXT, source_time TEXT NOT NULL, equity REAL, cash REAL,
  buying_power REAL, multiplier TEXT, positions_json TEXT, open_orders_json TEXT, market_json TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_arms (
  run_id TEXT NOT NULL, arm TEXT NOT NULL, as_of TEXT, target_weights_json TEXT, candidates_json TEXT,
  approved_json TEXT, intents_json TEXT, sim_equity REAL, sim_cash REAL, sim_positions_json TEXT,
  assumptions_json TEXT, is_orders_arm INTEGER, PRIMARY KEY (run_id, arm)
);
CREATE TABLE IF NOT EXISTS decision_divergence (
  run_id TEXT NOT NULL, arm TEXT NOT NULL, baseline_arm TEXT NOT NULL, as_of TEXT,
  rank_changes_json TEXT, membership_added_json TEXT, membership_removed_json TEXT, weight_distance REAL,
  orders_caused INTEGER, orders_prevented INTEGER, orders_resized INTEGER, notional_delta REAL,
  return_diff_json TEXT, PRIMARY KEY (run_id, arm, baseline_arm)
);
CREATE TABLE IF NOT EXISTS macro_observations (
  series TEXT NOT NULL, effective_date TEXT NOT NULL, value REAL, retrieved_at TEXT NOT NULL,
  available_at TEXT NOT NULL, source TEXT, PRIMARY KEY (series, effective_date)
);
CREATE TABLE IF NOT EXISTS fundamentals (
  symbol TEXT NOT NULL, concept TEXT NOT NULL, period_end TEXT NOT NULL, filed TEXT NOT NULL, form TEXT,
  fp TEXT, fy INTEGER, value REAL, unit TEXT, retrieved_at TEXT, PRIMARY KEY (symbol, concept, period_end, filed)
);
CREATE TABLE IF NOT EXISTS research_runs (
  as_of TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT, status TEXT, doc_watermark_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(date);
CREATE INDEX IF NOT EXISTS idx_orders_run ON orders(run_id);
CREATE INDEX IF NOT EXISTS idx_calls_run ON model_calls(run_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def q1(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    # ---- runs / stages ----------------------------------------------------
    def create_run(self, run_id: str, mode: str, as_of: str, code_version: str,
                   config_hash: str, strategy_version: str, replay_of: Optional[str] = None) -> None:
        self.x("INSERT INTO runs(run_id,mode,as_of,started_at,status,code_version,config_hash,strategy_version,replay_of)"
               " VALUES(?,?,?,?,?,?,?,?,?)",
               (run_id, mode, as_of, utcnow(), "running", code_version, config_hash, strategy_version, replay_of))

    def finish_run(self, run_id: str, status: str, error: Optional[str] = None) -> None:
        self.x("UPDATE runs SET finished_at=?, status=?, error=? WHERE run_id=?", (utcnow(), status, error, run_id))

    def stage_start(self, run_id: str, stage: str, input_hash: Optional[str] = None) -> None:
        self.x("INSERT OR REPLACE INTO run_stages(run_id,stage,status,started_at,input_hash) VALUES(?,?,?,?,?)",
               (run_id, stage, "running", utcnow(), input_hash))

    def stage_done(self, run_id: str, stage: str, artifact: Any, status: str = "ok",
                   error: Optional[str] = None) -> None:
        self.x("UPDATE run_stages SET status=?, finished_at=?, artifact_json=?, error=? WHERE run_id=? AND stage=?",
               (status, utcnow(), dumps(artifact), error, run_id, stage))

    def stage_artifact(self, run_id: str, stage: str) -> Optional[Any]:
        r = self.q1("SELECT artifact_json, status FROM run_stages WHERE run_id=? AND stage=?", (run_id, stage))
        if not r or r["status"] != "ok" or r["artifact_json"] is None:
            return None
        return json.loads(r["artifact_json"])

    def run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM runs WHERE run_id=?", (run_id,))

    # ---- bars ---------------------------------------------------------------
    def upsert_bars(self, rows: Iterable[Dict[str, Any]], source: str) -> int:
        now = utcnow()
        n = 0
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT OR REPLACE INTO bars(symbol,date,open,high,low,close,adj_close,volume,source,fetched_at)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (r["symbol"], r["date"], r["open"], r["high"], r["low"], r["close"],
                           r["adj_close"], r["volume"], source, now))
                n += 1
        return n

    def bars(self, symbols: Iterable[str], start: str, end: str) -> List[sqlite3.Row]:
        syms = list(symbols)
        marks = ",".join("?" * len(syms))
        return self.q(f"SELECT * FROM bars WHERE symbol IN ({marks}) AND date>=? AND date<=? ORDER BY symbol,date",
                      [*syms, start, end])

    # ---- events -------------------------------------------------------------
    def upsert_events(self, run_id: str, events: Iterable[Dict[str, Any]]) -> int:
        now = utcnow()
        n = 0
        with self.tx() as c:
            for e in events:
                c.execute("INSERT OR IGNORE INTO events(event_id,symbol,published_at,source,title,summary,url,content_hash,fetched_at,first_run_id)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (e["event_id"], e["symbol"], e["published_at"], e.get("source"), e.get("title"),
                           e.get("summary"), e.get("url"), e.get("content_hash"), now, run_id))
                c.execute("INSERT OR IGNORE INTO run_events(run_id,event_id) VALUES(?,?)", (run_id, e["event_id"]))
                n += 1
        return n

    def run_events(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self.q("SELECT e.* FROM events e JOIN run_events r ON r.event_id=e.event_id WHERE r.run_id=?"
                      " ORDER BY e.symbol, e.published_at", (run_id,))
        return [dict(r) for r in rows]

    # ---- ai cache / model calls -------------------------------------------
    def cache_get(self, input_hash: str) -> Optional[Dict[str, Any]]:
        r = self.q1("SELECT output_json FROM ai_cache WHERE input_hash=?", (input_hash,))
        return json.loads(r["output_json"]) if r else None

    def cache_put(self, input_hash: str, tier: str, provider: str, model: str, prompt_version: str,
                  schema_version: str, output: Any) -> None:
        self.x("INSERT OR REPLACE INTO ai_cache VALUES(?,?,?,?,?,?,?,?)",
               (input_hash, tier, provider, model, prompt_version, schema_version, dumps(output), utcnow()))

    def log_model_call(self, **kw: Any) -> None:
        cols = ["run_id", "tier", "provider", "model", "prompt_version", "schema_version", "input_hash", "cache_hit",
                "input_tokens", "output_tokens", "latency_ms", "cost_usd", "valid", "error", "output_json", "raw_text"]
        vals = [kw.get(c) for c in cols]
        if isinstance(vals[cols.index("output_json")], (dict, list)):
            vals[cols.index("output_json")] = dumps(vals[cols.index("output_json")])
        self.x(f"INSERT INTO model_calls({','.join(cols)},created_at) VALUES({','.join('?' * len(cols))},?)",
               [*vals, utcnow()])

    # ---- costs --------------------------------------------------------------
    def add_cost(self, date: str, category: str, usd: float, tokens_in: int = 0, tokens_out: int = 0,
                 run_id: Optional[str] = None, note: Optional[str] = None) -> None:
        self.x("INSERT INTO cost_ledger(date,category,usd,tokens_in,tokens_out,run_id,note) VALUES(?,?,?,?,?,?,?)",
               (date, category, usd, tokens_in, tokens_out, run_id, note))

    def cost_sum(self, category_prefix: str, date_from: str, date_to: str) -> Dict[str, float]:
        r = self.q1("SELECT COALESCE(SUM(usd),0) usd, COALESCE(SUM(tokens_in),0) tin, COALESCE(SUM(tokens_out),0) tout"
                    " FROM cost_ledger WHERE category LIKE ? AND date>=? AND date<=?",
                    (category_prefix + "%", date_from, date_to))
        return {"usd": float(r["usd"]), "tokens_in": int(r["tin"]), "tokens_out": int(r["tout"])}

    # ---- decisions ----------------------------------------------------------
    def save_decisions(self, run_id: str, decisions: Iterable[Dict[str, Any]]) -> None:
        with self.tx() as c:
            for d in decisions:
                c.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (run_id, d["symbol"], d.get("as_of"), d.get("horizon_days"), d.get("quant_score"),
                           d.get("ai_score"), d.get("ensemble_score"), d.get("confidence"),
                           dumps(d.get("risk_flags", [])), dumps(d.get("evidence_ids", [])), d.get("prompt_version"),
                           d.get("rank"), d.get("proposed_weight"), d.get("approved_weight"),
                           int(bool(d.get("eligible", True))), dumps(d.get("features", {}))))

    # ---- orders -------------------------------------------------------------
    def upsert_order(self, o: Dict[str, Any]) -> None:
        self.x("INSERT OR REPLACE INTO orders(client_order_id,run_id,symbol,side,qty,notional,limit_price,order_type,status,"
               "broker_order_id,submitted_at,filled_qty,filled_avg_price,fees,updated_at,raw_json)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (o["client_order_id"], o["run_id"], o["symbol"], o["side"], o.get("qty"), o.get("notional"),
                o.get("limit_price"), o.get("order_type", "limit"), o["status"], o.get("broker_order_id"),
                o.get("submitted_at"), o.get("filled_qty", 0.0), o.get("filled_avg_price"), o.get("fees", 0.0),
                utcnow(), dumps(o.get("raw", {}))))

    def order(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        r = self.q1("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,))
        return dict(r) if r else None

    def orders_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT * FROM orders WHERE run_id=? ORDER BY symbol", (run_id,))]

    def open_orders(self) -> List[Dict[str, Any]]:
        """Every locally-recorded order whose broker status is still open. The status set MUST be the
        broker's (base.OPEN_STATUSES): on 2026-09-09 'pending_new' was missing here, so fills were never
        counted and reconciliation raised a false mismatch."""
        from .broker.base import OPEN_STATUSES
        marks = ",".join("?" * len(OPEN_STATUSES))
        return [dict(r) for r in self.q(f"SELECT * FROM orders WHERE status IN ({marks})", sorted(OPEN_STATUSES))]

    # ---- equity / safe mode / kv ------------------------------------------
    def save_equity(self, date: str, mode: str, equity: float, cash: float, positions: Dict[str, Any],
                    hwm: float, sod_equity: float, benchmark_close: Optional[float], source: str) -> None:
        self.x("INSERT OR REPLACE INTO equity_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
               (date, mode, equity, cash, dumps(positions), hwm, sod_equity, benchmark_close, source, utcnow()))

    def equity_history(self, mode: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT * FROM equity_snapshots WHERE mode=? ORDER BY date", (mode,))]

    def latest_equity(self, mode: str) -> Optional[Dict[str, Any]]:
        r = self.q1("SELECT * FROM equity_snapshots WHERE mode=? ORDER BY date DESC LIMIT 1", (mode,))
        return dict(r) if r else None

    def enter_safe_mode(self, reason: str, run_id: Optional[str] = None) -> None:
        if not self.safe_mode_active():
            self.x("INSERT INTO safe_mode(activated_at,reason,run_id) VALUES(?,?,?)", (utcnow(), reason, run_id))

    def safe_mode_active(self) -> Optional[Dict[str, Any]]:
        r = self.q1("SELECT * FROM safe_mode WHERE cleared_at IS NULL ORDER BY id DESC LIMIT 1")
        return dict(r) if r else None

    def clear_safe_mode(self, who: str) -> int:
        with self._lock:
            cur = self._conn.execute("UPDATE safe_mode SET cleared_at=?, cleared_by=? WHERE cleared_at IS NULL", (utcnow(), who))
            return cur.rowcount

    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.q1("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self.x("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, dumps(value)))

    def save_reconciliation(self, run_id: str, ok: bool, mismatches: List[Dict[str, Any]]) -> None:
        self.x("INSERT OR REPLACE INTO reconciliations VALUES(?,?,?,?)", (run_id, int(ok), dumps(mismatches), utcnow()))

    # ---- V2: documents / events / claims ----------------------------------
    def doc_exists(self, doc_id: str) -> Optional[Dict[str, Any]]:
        r = self.q1("SELECT * FROM source_documents WHERE doc_id=?", (doc_id,))
        return dict(r) if r else None

    def doc_by_hash(self, content_hash: str) -> Optional[Dict[str, Any]]:
        r = self.q1("SELECT * FROM source_documents WHERE content_hash=? LIMIT 1", (content_hash,))
        return dict(r) if r else None

    def upsert_document(self, d: Dict[str, Any], run_id: Optional[str]) -> str:
        """Append-only: an existing doc_id with a different hash gets a revision record, never an overwrite."""
        prev = self.doc_exists(d["doc_id"])
        if prev:
            if prev["content_hash"] != d["content_hash"]:
                self.x("INSERT INTO document_revisions(doc_id,prior_hash,new_hash,revised_at,relationship) VALUES(?,?,?,?,?)",
                       (d["doc_id"], prev["content_hash"], d["content_hash"], utcnow(), "provider_update"))
                self.x("UPDATE source_documents SET content_hash=?, title=?, summary=?, content_ref=?, meta_json=? WHERE doc_id=?",
                       (d["content_hash"], d.get("title"), d.get("summary"), d.get("content_ref"), dumps(d.get("meta", {})), d["doc_id"]))
                return "revised"
            return "existing"
        self.x("INSERT INTO source_documents(doc_id,source,provider_id,doc_type,symbols_json,primary_symbol,title,summary,content_ref,url,"
               "event_time,published_at,retrieved_at,available_at,effective_date,content_hash,source_quality,license_flags,quarantined,"
               "quarantine_reason,meta_json,first_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (d["doc_id"], d["source"], d.get("provider_id"), d["doc_type"], dumps(d.get("symbols", [])), d.get("primary_symbol"),
                d.get("title"), d.get("summary"), d.get("content_ref"), d.get("url"), d.get("event_time"), d["published_at"],
                d["retrieved_at"], d["available_at"], d.get("effective_date"), d["content_hash"], d.get("source_quality"),
                d.get("license_flags"), int(bool(d.get("quarantined"))), d.get("quarantine_reason"), dumps(d.get("meta", {})), run_id))
        return "new"

    def documents(self, since: str, until: str, symbols: Optional[Iterable[str]] = None,
                  include_quarantined: bool = False) -> List[Dict[str, Any]]:
        rows = self.q("SELECT * FROM source_documents WHERE available_at>=? AND available_at<=? " +
                      ("" if include_quarantined else "AND quarantined=0 ") + "ORDER BY available_at", (since, until))
        out = [dict(r) for r in rows]
        for d in out:
            d["symbols"] = json.loads(d.pop("symbols_json") or "[]")
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
        if symbols is not None:
            ss = set(symbols)
            out = [d for d in out if ss & set(d["symbols"])]
        return out

    def add_claims(self, claims: Iterable[Dict[str, Any]], run_id: str) -> int:
        n = 0
        with self.tx() as c:
            for cl in claims:
                c.execute("INSERT OR IGNORE INTO atomic_claims(claim_id,doc_id,symbol,claim,evidence_span,quantity,claim_time,certainty,"
                          "event_type,created_at,run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (cl["claim_id"], cl["doc_id"], cl.get("symbol"), cl["claim"], cl.get("evidence_span"), cl.get("quantity"),
                           cl.get("claim_time"), cl.get("certainty"), cl.get("event_type"), utcnow(), run_id))
                n += 1
        return n

    def claims_for_docs(self, doc_ids: Iterable[str]) -> List[Dict[str, Any]]:
        ids = list(doc_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return [dict(r) for r in self.q(f"SELECT * FROM atomic_claims WHERE doc_id IN ({marks})", ids)]

    def upsert_event(self, e: Dict[str, Any]) -> None:
        now = utcnow()
        prev = self.q1("SELECT event_id, created_at, first_run_id FROM canonical_events WHERE event_id=?", (e["event_id"],))
        self.x("INSERT OR REPLACE INTO canonical_events(event_id,event_type,primary_symbol,related_symbols_json,event_time,available_at,"
               "status,novelty,source_quality,contradiction_level,importance,sentiment,fingerprint,title,document_ids_json,claim_ids_json,"
               "supersedes_event_id,created_at,updated_at,first_run_id,last_run_id,routing_class,actionable,audit_json)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (e["event_id"], e["event_type"], e["primary_symbol"], dumps(e.get("related_symbols", [])), e.get("event_time"),
                e["available_at"], e.get("status", "candidate"), e.get("novelty"), e.get("source_quality"), e.get("contradiction_level"),
                e.get("importance"), e.get("sentiment"), e.get("fingerprint"), e.get("title"), dumps(e.get("document_ids", [])),
                dumps(e.get("claim_ids", [])), e.get("supersedes_event_id"), prev["created_at"] if prev else now, now,
                prev["first_run_id"] if prev else e.get("run_id"), e.get("run_id"), e.get("routing_class"),
                int(bool(e.get("actionable"))), dumps(e.get("audit", {}))))
        with self.tx() as c:
            for cid in e.get("claim_ids", []):
                c.execute("INSERT OR IGNORE INTO event_claim_links(event_id,claim_id,role) VALUES(?,?,?)", (e["event_id"], cid, "supports"))

    def events(self, since: str, until: str, symbols: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        rows = self.q("SELECT * FROM canonical_events WHERE available_at>=? AND available_at<=? ORDER BY available_at", (since, until))
        out = []
        for r in rows:
            d = dict(r)
            d["related_symbols"] = json.loads(d.pop("related_symbols_json") or "[]")
            d["document_ids"] = json.loads(d.pop("document_ids_json") or "[]")
            d["claim_ids"] = json.loads(d.pop("claim_ids_json") or "[]")
            d["audit"] = json.loads(d.pop("audit_json") or "{}")
            out.append(d)
        if symbols is not None:
            ss = set(symbols)
            out = [d for d in out if d["primary_symbol"] in ss or ss & set(d["related_symbols"])]
        return out

    # ---- V2: features / forecasts / outcomes ------------------------------
    def save_feature_snapshots(self, as_of: str, version: str, rows: Iterable[Dict[str, Any]], run_id: str) -> None:
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT OR REPLACE INTO feature_snapshots VALUES(?,?,?,?,?,?)",
                          (as_of, r["symbol"], version, dumps(r["values"]), dumps(r.get("missing", [])), run_id))

    def save_forecast(self, f: Dict[str, Any]) -> None:
        self.x("INSERT OR IGNORE INTO forecasts(forecast_id,run_id,arm,symbol,as_of,decision_time,forecast_version,horizons_json,raw_json,"
               "calibrated_json,evidence_quality,model_disagreement,abstain,evidence_ids_json,risk_flags_json,frozen_at,overlay_score,"
               "base_price,benchmark_base) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f["forecast_id"], f["run_id"], f["arm"], f["symbol"], f["as_of"], f["decision_time"], f.get("forecast_version"),
                dumps(f["horizons"]), dumps(f.get("raw", {})), dumps(f.get("calibrated", {})), f.get("evidence_quality"),
                f.get("model_disagreement"), int(bool(f.get("abstain"))), dumps(f.get("evidence_ids", [])),
                dumps(f.get("risk_flags", [])), utcnow(), f.get("overlay_score"), f.get("base_price"), f.get("benchmark_base")))

    def unscored_forecasts(self, horizon: int) -> List[Dict[str, Any]]:
        rows = self.q("SELECT f.* FROM forecasts f LEFT JOIN forecast_outcomes o ON o.forecast_id=f.forecast_id AND o.horizon=? "
                      "WHERE o.forecast_id IS NULL ORDER BY f.as_of", (horizon,))
        return [dict(r) for r in rows]

    def save_outcome(self, o: Dict[str, Any]) -> None:
        self.x("INSERT OR REPLACE INTO forecast_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (o["forecast_id"], o["horizon"], utcnow(), o.get("matured_date"), o.get("realized_return"), o.get("benchmark_return"),
                o.get("excess_return"), o.get("p_positive"), o.get("hit"), o.get("brier"), o.get("status", "scored")))

    def scored_outcomes(self, horizon: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT o.*, f.symbol, f.arm, f.as_of, f.evidence_quality FROM forecast_outcomes o JOIN forecasts f ON f.forecast_id=o.forecast_id "
               "WHERE o.status='scored'")
        params: list = []
        if horizon is not None:
            sql += " AND o.horizon=?"
            params.append(horizon)
        return [dict(r) for r in self.q(sql + " ORDER BY f.as_of", params)]

    def active_calibration(self, horizon: int) -> Optional[Dict[str, Any]]:
        r = self.q1("SELECT * FROM calibration_models WHERE horizon=? AND active=1 ORDER BY id DESC LIMIT 1", (horizon,))
        return dict(r) if r else None

    def save_calibration(self, horizon: int, version: str, n: int, params: Dict[str, Any], metrics: Dict[str, Any]) -> None:
        self.x("UPDATE calibration_models SET active=0 WHERE horizon=?", (horizon,))
        self.x("INSERT INTO calibration_models(horizon,version,trained_at,n_samples,params_json,metrics_json,active) VALUES(?,?,?,?,?,?,1)",
               (horizon, version, utcnow(), n, dumps(params), dumps(metrics)))

    # ---- V2: health / notifications / snapshots / arms --------------------
    def add_health_event(self, severity: str, component: str, reason: str, run_id: Optional[str] = None,
                         remediation: Optional[str] = None) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO health_events(created_at,severity,component,reason,run_id,remediation) VALUES(?,?,?,?,?,?)",
                                     (utcnow(), severity, component, reason[:2000], run_id, remediation))
            return int(cur.lastrowid)

    def health_events_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT * FROM health_events WHERE run_id=? ORDER BY id", (run_id,))]

    def add_notification_attempt(self, health_event_id: int, channel: str, destination_hash: str, status: str,
                                 attempt: int, error: Optional[str]) -> None:
        self.x("INSERT INTO notification_attempts(health_event_id,channel,destination_hash,status,attempt,error,created_at) VALUES(?,?,?,?,?,?,?)",
               (health_event_id, channel, destination_hash, status, attempt, error, utcnow()))

    def save_broker_snapshot(self, run_id: str, mode: str, snap: Dict[str, Any]) -> None:
        self.x("INSERT INTO broker_snapshots(run_id,mode,source_time,equity,cash,buying_power,multiplier,positions_json,open_orders_json,market_json)"
               " VALUES(?,?,?,?,?,?,?,?,?,?)",
               (run_id, mode, snap.get("source_time") or utcnow(), snap.get("equity"), snap.get("cash"), snap.get("buying_power"),
                str(snap.get("multiplier")), dumps(snap.get("positions", {})), dumps(snap.get("open_orders", [])), dumps(snap.get("market", {}))))

    def save_arm(self, run_id: str, arm: str, as_of: str, payload: Dict[str, Any]) -> None:
        self.x("INSERT OR REPLACE INTO portfolio_arms VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (run_id, arm, as_of, dumps(payload.get("target_weights", {})), dumps(payload.get("candidates", [])),
                dumps(payload.get("approved", {})), dumps(payload.get("intents", [])), payload.get("sim_equity"),
                payload.get("sim_cash"), dumps(payload.get("sim_positions", {})), dumps(payload.get("assumptions", {})),
                int(bool(payload.get("is_orders_arm")))))

    def arm_history(self, arm: str, mode_prefix: str = "") -> List[Dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT run_id, as_of, sim_equity, sim_cash FROM portfolio_arms WHERE arm=? AND sim_equity IS NOT NULL"
                                        " AND run_id LIKE ? ORDER BY as_of", (arm, f"%{mode_prefix}%"))]

    def save_divergence(self, run_id: str, arm: str, baseline: str, as_of: str, d: Dict[str, Any]) -> None:
        self.x("INSERT OR REPLACE INTO decision_divergence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (run_id, arm, baseline, as_of, dumps(d.get("rank_changes", {})), dumps(d.get("membership_added", [])),
                dumps(d.get("membership_removed", [])), d.get("weight_distance"), d.get("orders_caused"), d.get("orders_prevented"),
                d.get("orders_resized"), d.get("notional_delta"), dumps(d.get("return_diff", {}))))

    def upsert_macro(self, rows: Iterable[Dict[str, Any]]) -> int:
        n = 0
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT OR IGNORE INTO macro_observations VALUES(?,?,?,?,?,?)",
                          (r["series"], r["effective_date"], r["value"], r["retrieved_at"], r["available_at"], r.get("source", "fred")))
                n += 1
        return n

    def macro_series(self, series: str, until: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.q("SELECT * FROM macro_observations WHERE series=? AND available_at<=? ORDER BY effective_date",
                                        (series, until))]

    def upsert_fundamentals(self, rows: Iterable[Dict[str, Any]]) -> int:
        n = 0
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT OR IGNORE INTO fundamentals VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (r["symbol"], r["concept"], r["period_end"], r["filed"], r.get("form"), r.get("fp"), r.get("fy"),
                           r["value"], r.get("unit"), utcnow()))
                n += 1
        return n

    def fundamentals_asof(self, symbol: str, concept: str, as_of: str) -> List[Dict[str, Any]]:
        """Point-in-time: only rows FILED on or before as_of; latest filing per period wins."""
        rows = self.q("SELECT * FROM fundamentals WHERE symbol=? AND concept=? AND filed<=? ORDER BY period_end, filed",
                      (symbol, concept, as_of))
        latest: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            latest[r["period_end"]] = dict(r)
        return [latest[k] for k in sorted(latest)]

    def research_run(self, as_of: str) -> Optional[Dict[str, Any]]:
        r = self.q1("SELECT * FROM research_runs WHERE as_of=?", (as_of,))
        return dict(r) if r else None

    def save_research_run(self, as_of: str, run_id: str, status: str, watermark: Dict[str, Any]) -> None:
        self.x("INSERT OR REPLACE INTO research_runs VALUES(?,?,?,?,?)", (as_of, run_id, utcnow(), status, dumps(watermark)))

    def completed_sessions(self, mode: str) -> int:
        r = self.q1("SELECT COUNT(DISTINCT as_of) n FROM runs WHERE mode=? AND status='ok'", (mode,))
        return int(r["n"]) if r else 0
