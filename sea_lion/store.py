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

    def completed_sessions(self, mode: str) -> int:
        r = self.q1("SELECT COUNT(DISTINCT as_of) n FROM runs WHERE mode=? AND status='ok'", (mode,))
        return int(r["n"]) if r else 0
