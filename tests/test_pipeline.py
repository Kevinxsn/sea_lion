"""End-to-end pipeline tests on synthetic data with fake market data and a fake LLM."""
from datetime import date

import pytest

from conftest import FakeEvents, FakeMarketData, FakeProvider, make_events, synthetic_bars
from sea_lion import pipeline as P
from sea_lion.ai import router as RT
from sea_lion.store import Store


@pytest.fixture
def fake_llm(monkeypatch):
    prov = FakeProvider(impact=0.6, confidence=0.9)
    monkeypatch.setattr(RT, "build_provider", lambda *a, **k: prov)
    return prov


def _pipe(cfg, store, bars, events=None):
    md = FakeMarketData(bars)
    es = FakeEvents(events or [])
    return P.Pipeline(cfg, store, market_data=md, event_source=es), md


def test_sim_three_sessions_end_to_end(cfg, fake_llm):
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=320, end=date(2026, 8, 28))
    dates = sorted(bars["date"].unique())
    d1, d2, d3 = dates[-3], dates[-2], dates[-1]
    pipe, md = _pipe(cfg, store, bars, make_events(["AAPL", "MSFT", "NVDA"], date.fromisoformat(d1)))
    r1 = pipe.run(as_of=date.fromisoformat(d1))
    assert r1["status"] == "ok", r1
    ex = store.stage_artifact(r1["run_id"], "execute")
    assert ex["submitted"], "sim mode should submit orders on day 1"
    ai = store.stage_artifact(r1["run_id"], "ai")
    assert ai["available"] and ai["facts"] and ai["scores"]
    # AI contribution is bounded and observable
    prop = store.stage_artifact(r1["run_id"], "propose")
    assert all(abs(v) <= 0.2 + 1e-9 for v in prop["ai_contribution"].values())
    assert prop["quant_only_weights"] is not None
    # second run same day is skipped (no duplicate session)
    assert pipe.run(as_of=date.fromisoformat(d1))["status"] == "skipped_already_completed"
    # day 2: orders fill at the open, reconcile OK, equity snapshot recorded
    pipe2, _ = _pipe(cfg, store, bars, [])
    r2 = pipe2.run(as_of=date.fromisoformat(d2))
    assert r2["status"] == "ok", r2
    acct = store.stage_artifact(r2["run_id"], "account")
    assert acct["positions"], "fills should create positions"
    assert acct["reconcile"]["ok"] and not store.safe_mode_active()
    filled = [o for o in store.orders_for_run(r1["run_id"]) if o["status"] == "filled"]
    assert filled
    r3 = _pipe(cfg, store, bars, [])[0].run(as_of=date.fromisoformat(d3))
    assert r3["status"] == "ok"
    hist = store.equity_history("sim")
    assert [h["date"] for h in hist] == [d1, d2, d3]
    assert store.completed_sessions("sim") == 3
    # report files exist
    assert r3["reports"]["html"] and r3["reports"]["json"]


def test_replay_reproduces_proposal(cfg, fake_llm):
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = max(bars["date"])
    pipe, md = _pipe(cfg, store, bars, make_events(["AAPL", "MSFT"], date.fromisoformat(d)))
    r = pipe.run(as_of=date.fromisoformat(d))
    assert r["status"] == "ok"
    n_calls_before = len(fake_llm.calls)
    # replay with a market-data source that would return DIFFERENT data if consulted
    other = synthetic_bars(n_days=300, seed=99)
    pipe2, md2 = _pipe(cfg, store, other, [])
    rr = pipe2.run(replay_of=r["run_id"])
    assert rr["replay"]["identical"], rr["replay"]
    assert md2.calls == 0 and len(fake_llm.calls) == n_calls_before   # no network, no model calls
    assert store.stage_artifact(rr["run_id"], "execute")["orders"] == "disabled"


def test_resume_after_crash_does_not_duplicate_orders(cfg, fake_llm, monkeypatch):
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, _ = _pipe(cfg, store, bars, [])
    orig = P.Pipeline.st_execute

    def crash_after_submit(self, run_id, ctx, **kw):
        orig(self, run_id, ctx, **kw)
        raise RuntimeError("simulated crash after submit")
    monkeypatch.setattr(P.Pipeline, "st_execute", crash_after_submit)
    r = pipe.run(as_of=d)
    assert r["status"] == "error"
    n_orders = len(pipe.broker().s["orders"])
    assert n_orders > 0
    monkeypatch.setattr(P.Pipeline, "st_execute", orig)
    pipe2 = P.Pipeline(cfg, store, market_data=FakeMarketData(bars), event_source=FakeEvents([]))
    r2 = pipe2.run(resume_run_id=r["run_id"])
    assert r2["status"] == "ok"
    assert len(pipe2.broker().s["orders"]) == n_orders
    ex = store.stage_artifact(r["run_id"], "execute")
    assert ex["duplicates_prevented"] == n_orders and not ex["submitted"]


def test_stale_data_aborts_before_models_and_orders(cfg, fake_llm):
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=300, end=date(2026, 8, 1))
    pipe, _ = _pipe(cfg, store, bars, make_events(["AAPL"], date(2026, 8, 28)))
    r = pipe.run(as_of=date(2026, 8, 28), force=True)
    assert r["status"] == "aborted_stale_data"
    assert fake_llm.calls == [] and store.orders_for_run(r["run_id"]) == []


def test_ai_unavailable_is_neutral_and_blocks_orders_in_paper_only(cfg, monkeypatch):
    prov = FakeProvider(fail=True)
    monkeypatch.setattr(RT, "build_provider", lambda *a, **k: prov)
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, _ = _pipe(cfg, store, bars, make_events(["AAPL"], d))
    r = pipe.run(as_of=d)
    assert r["status"] == "ok"
    ai = store.stage_artifact(r["run_id"], "ai")
    assert ai["available"] is False and ai["scores"] == {}
    prop = store.stage_artifact(r["run_id"], "propose")
    assert prop["weights"] == prop["quant_only_weights"]
    assert store.stage_artifact(r["run_id"], "execute")["submitted"]   # sim allows quant-only fallback
    cfg.ai.quant_only_orders_allowed_modes = []
    r2 = pipe.run(as_of=d, force=True)
    risk = store.stage_artifact(r2["run_id"], "risk")
    assert "ai_unavailable_orders_blocked" in risk["global_flags"] and risk["intents"] == []


def test_reconcile_mismatch_enters_safe_mode(cfg, fake_llm):
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=300)
    dates = sorted(bars["date"].unique())
    pipe, _ = _pipe(cfg, store, bars, [])
    assert pipe.run(as_of=date.fromisoformat(dates[-2]))["status"] == "ok"
    # someone trades in the account behind our back
    st = store.kv_get("sim_state:sim")
    st["positions"]["GLD"] = {"qty": 3.0, "avg_price": 100.0}
    store.kv_set("sim_state:sim", st)
    r = _pipe(cfg, store, bars, [])[0].run(as_of=date.fromisoformat(dates[-1]))
    assert r["status"] == "ok"
    assert store.safe_mode_active() and "mismatch" in store.safe_mode_active()["reason"]
    risk = store.stage_artifact(r["run_id"], "risk")
    assert "safe_mode_active" in risk["global_flags"]
    assert not [i for i in risk["intents"] if i["side"] == "buy"]


def test_shadow_mode_never_submits(cfg, fake_llm):
    cfg.run.mode = "shadow"
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    r = _pipe(cfg, store, bars, [])[0].run(as_of=d)
    assert r["status"] == "ok"
    ex = store.stage_artifact(r["run_id"], "execute")
    assert ex["orders"] == "disabled" and ex["would_submit"]
    assert store.orders_for_run(r["run_id"]) == []


def test_live_gate_blocks_without_flags(cfg, fake_llm, monkeypatch):
    cfg.run.mode = "live"
    cfg.broker.provider = "sim"
    monkeypatch.delenv("SEA_LION_LIVE_ENABLED", raising=False)
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=300)
    r = _pipe(cfg, store, bars, [])[0].run(as_of=date.fromisoformat(max(bars["date"])))
    assert r["status"] == "live_blocked"
    monkeypatch.setenv("SEA_LION_LIVE_ENABLED", "1")
    monkeypatch.setenv("SEA_LION_LIVE_CONFIRM_TOKEN", f"LIVE-{date.today().isoformat()}")
    r2 = _pipe(cfg, store, bars, [])[0].run(as_of=date.fromisoformat(max(bars["date"])), force=True)
    assert r2["status"] == "live_blocked" and "paper sessions" in r2["error"]
