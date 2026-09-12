"""V2 end-to-end: documents -> claims -> events -> passes -> forecasts -> arms -> risk -> execute, plus
reconcile-only on skipped sessions, attention/summary, dry-run, cutoff replay, and degradation."""
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import FakeDocs, FakeEvents, FakeMarketData, FakeProvider, make_docs, synthetic_bars
from sea_lion import notify as N, pipeline as P
from sea_lion.ai import router as RT
from sea_lion.calendar import TradingCalendar
from sea_lion.store import Store


@pytest.fixture
def v2cfg(cfg):
    cfg.v2.enabled = True
    cfg.v2.arms.enabled = True
    cfg.v2.arms.orders_arm = "C"
    cfg.v2.sources.sec_enabled = False
    cfg.v2.sources.fred_enabled = False
    cfg.v2.sources.fundamentals_enabled = False
    cfg.notify.transport = "fake"
    cfg.notify.email_to = ["owner@example.com"]
    cfg.v2.research.concurrency = 1
    return cfg


def _pipe(cfg, store, bars, docs, prov_kwargs=None, monkeypatch=None):
    prov = FakeProvider(**(prov_kwargs or {}))
    if monkeypatch is not None:
        monkeypatch.setattr(RT, "build_provider", lambda *a, **k: prov)
    cal = TradingCalendar(store, provider="fallback")
    p = P.Pipeline(cfg, store, market_data=FakeMarketData(bars), event_source=FakeEvents([]), doc_sources=[FakeDocs(docs)], calendar=cal)
    return p, prov


def test_v2_sim_cycle_arm_c_orders_and_shadow_arms(v2cfg, monkeypatch):
    N.SENT.clear()
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=320, end=date(2026, 8, 28))
    d = date(2026, 8, 28)
    docs = make_docs(["AAPL", "MSFT", "NVDA", "JPM"], d)
    pipe, prov = _pipe(v2cfg, store, bars, docs, monkeypatch=monkeypatch)
    r = pipe.run(as_of=d)
    assert r["status"] == "ok" and r["run_outcome"] == "completed", r
    rid = r["run_id"]
    ing = store.stage_artifact(rid, "ingest")
    assert ing["n_documents"] == 4 and ing["doc_stats"]["sources"]["fake_docs"]["new"] == 4
    v2 = store.stage_artifact(rid, "v2_research")
    assert v2["extraction"]["n_claims"] == 4 and v2["n_events"] == 4
    assert all(e["routing_class"] in ("corporate_event", "company_news") and e["actionable"] for e in v2["events"])
    assert set(v2["candidates"]) == {"AAPL", "MSFT", "NVDA", "JPM"}
    for sym, pk in v2["forecasts"].items():
        assert pk["synth"] and not pk["abstain"] and pk["supported_claim_ids"]
        assert pk["calibration_mode"] == "shrunk" and 0 < pk["overlay"] <= 1
    assert set(v2["scores"]) == {"AAPL", "MSFT", "NVDA", "JPM"}
    # every pass was logged under its own label; a forecast was frozen per candidate
    tiers = {r["tier"] for r in store.q("SELECT DISTINCT tier FROM model_calls WHERE run_id=?", (rid,))}
    assert {"v2.extract", "v2.audit", "v2.analyst", "v2.skeptic", "v2.context", "v2.synth"} <= tiers
    assert store.q1("SELECT COUNT(*) n FROM forecasts WHERE run_id=? AND arm='C'", (rid,))["n"] == 4
    # arms recorded from the same cutoff; C is the orders arm; divergence vs A recorded
    arms = {r["arm"]: dict(r) for r in store.q("SELECT * FROM portfolio_arms WHERE run_id=?", (rid,))}
    assert set(arms) == {"A", "B", "C"} and arms["C"]["is_orders_arm"] == 1 and arms["A"]["sim_equity"] == 2000.0
    div = {r["arm"]: dict(r) for r in store.q("SELECT * FROM decision_divergence WHERE run_id=?", (rid,))}
    assert set(div) == {"B", "C"} and div["C"]["weight_distance"] is not None
    prop = store.stage_artifact(rid, "propose")
    assert prop["orders_arm"] == "C" and all(abs(v) <= 0.2 + 1e-9 for v in prop["ai_contribution"].values())
    ex = store.stage_artifact(rid, "execute")
    assert ex["submitted"]
    summ = store.stage_artifact(rid, "summary")
    assert summ["run_outcome"] == "completed" and summ["attention"] is False and summ["orders_arm"] == "C"
    assert r["summary_line"].startswith("SEA_LION_SUMMARY")
    assert N.SENT == []
    # report file is named by invocation date and outcome
    assert "_completed_" in r["reports"]["html"]


def test_unverifiable_span_is_dropped_and_no_evidence_means_abstain(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    docs = make_docs(["AAPL"], d, titles={"AAPL": "AAPL beats estimates INVENTED_SPAN"})
    pipe, prov = _pipe(v2cfg, store, bars, docs, monkeypatch=monkeypatch)
    r = pipe.run(as_of=d)
    v2 = store.stage_artifact(r["run_id"], "v2_research")
    assert v2["extraction"]["dropped_unverifiable_spans"] == 1 and v2["extraction"]["n_claims"] == 1
    store2 = Store(v2cfg.db_path.parent / "b.db")
    pipe2, _ = _pipe(v2cfg, store2, bars, make_docs(["AAPL"], d), {"synth_no_evidence": True}, monkeypatch)
    r2 = pipe2.run(as_of=d, force=True)
    v2b = store2.stage_artifact(r2["run_id"], "v2_research")
    pk = v2b["forecasts"]["AAPL"]
    assert pk["abstain"] and pk["abstain_reason"] == "no_evidence_cited" and v2b["scores"]["AAPL"]["score"] == 0.0


def test_disagreement_forces_abstain(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, _ = _pipe(v2cfg, store, bars, make_docs(["MSFT"], d), {"p_analyst": 0.9, "p_skeptic": 0.2}, monkeypatch)
    r = pipe.run(as_of=d)
    pk = store.stage_artifact(r["run_id"], "v2_research")["forecasts"]["MSFT"]
    assert pk["disagreement"] == pytest.approx(0.7) and pk["abstain"] and pk["abstain_reason"].startswith("disagreement")
    fc = store.q1("SELECT abstain, overlay_score FROM forecasts WHERE run_id=?", (r["run_id"],))
    assert fc["abstain"] == 1 and fc["overlay_score"] == 0.0


def test_macro_and_rumor_are_not_actionable(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    docs = make_docs(["XOM", "CVX"], d, titles={"XOM": "Fed signals rates on hold; oil steady", "CVX": "Rumor: CVX may be exploring a deal"})
    pipe, _ = _pipe(v2cfg, store, bars, docs, monkeypatch=monkeypatch)
    r = pipe.run(as_of=d)
    v2 = store.stage_artifact(r["run_id"], "v2_research")
    by = {e["primary_symbol"]: e for e in v2["events"]}
    assert by["XOM"]["routing_class"] == "macro" and not by["XOM"]["actionable"]
    assert by["CVX"]["routing_class"] == "rumor" and not by["CVX"]["actionable"]
    assert v2["candidates"] == [] and v2["scores"] == {}
    prop = store.stage_artifact(r["run_id"], "propose")
    assert prop["weights"] == prop["quant_only_weights"]


def test_skipped_session_still_reconciles_and_reports(v2cfg, monkeypatch):
    N.SENT.clear()
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, _ = _pipe(v2cfg, store, bars, make_docs(["AAPL"], d), monkeypatch=monkeypatch)
    assert pipe.run(as_of=d)["status"] == "ok"
    r2 = pipe.run(as_of=d)
    assert r2["status"] == "skipped_already_completed" and r2["run_outcome"] == "reconcile_only"
    assert store.stage_artifact(r2["run_id"], "reconcile_only")["ok"] is True
    assert store.q1("SELECT COUNT(*) n FROM broker_snapshots WHERE run_id=?", (r2["run_id"],))["n"] == 1
    assert "_reconcile_only_" in r2["reports"]["html"] and r2["summary"]["attention"] is False
    assert N.SENT == []                       # holidays/skips are not alerts


def test_safe_mode_and_ai_fallback_raise_attention_and_notify(v2cfg, monkeypatch):
    N.SENT.clear()
    v2cfg.run.mode = "paper"
    v2cfg.broker.provider = "sim"
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, prov = _pipe(v2cfg, store, bars, make_docs(["AAPL"], d), {"fail": True}, monkeypatch)
    r = pipe.run(as_of=d)
    assert r["status"] == "ok" and r["summary"]["attention"] is True
    assert "ai_unavailable_paper_live" in r["summary"]["attention_flags"]
    assert any("model layer unavailable" in m["body"] for m in N.SENT)
    N.SENT.clear()
    store.enter_safe_mode("manual test")
    r2 = pipe.run(as_of=d, force=True)
    assert r2["run_outcome"] == "safe_mode" and "safe_mode" in r2["summary"]["attention_flags"]


def test_dry_run_submits_nothing(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, _ = _pipe(v2cfg, store, bars, make_docs(["AAPL"], d), monkeypatch=monkeypatch)
    r = pipe.run(as_of=d, dry_run=True)
    assert r["run_outcome"] == "dry_run"
    ex = store.stage_artifact(r["run_id"], "execute")
    assert ex["orders"] == "disabled" and ex["would_submit"] and store.orders_for_run(r["run_id"]) == []


def test_replay_respects_cutoff_and_reproduces(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, prov = _pipe(v2cfg, store, bars, make_docs(["AAPL", "MSFT"], d), monkeypatch=monkeypatch)
    r = pipe.run(as_of=d)
    n_calls = len(prov.calls)
    # a document published AFTER the original cutoff must not enter the replay
    late = make_docs(["NVDA"], d + timedelta(days=1))
    pipe2, prov2 = _pipe(v2cfg, store, bars, late, monkeypatch=monkeypatch)
    rr = pipe2.run(replay_of=r["run_id"])
    assert rr["replay"]["identical"], rr["replay"]
    assert prov2.calls == [] and len(prov.calls) == n_calls
    v2 = store.stage_artifact(rr["run_id"], "v2_research")
    assert "NVDA" not in v2.get("scores", {})


def test_research_then_morning_reuses_and_processes_delta(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    docs = make_docs(["AAPL", "MSFT"], d)
    pipe, prov = _pipe(v2cfg, store, bars, docs, monkeypatch=monkeypatch)
    rr = pipe.research(as_of=d)
    assert rr["status"] == "ok" and store.research_run(d.isoformat())["status"] == "ok"
    n_after_research = len(prov.calls)
    # overnight: a new material doc for MSFT arrives; morning run must reuse research and rerun only MSFT
    src = pipe.doc_sources()[0]
    late = make_docs(["MSFT"], d, titles={"MSFT": "MSFT earnings call: guidance raised again"})[0]
    late_ts = (datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23)).isoformat()
    late.doc_id = "doc_MSFT_late"; late.available_at = late.published_at = late.retrieved_at = late_ts
    src.docs.append(late)
    r = pipe.run(as_of=d)
    v2 = store.stage_artifact(r["run_id"], "v2_research")
    assert v2["reused_from_research"] == rr["run_id"]
    assert v2["delta"]["n_docs"] == 1 and v2["delta"]["reran"] == ["MSFT"]
    assert len(prov.calls) - n_after_research < n_after_research     # only the delta was processed
    ai = store.stage_artifact(r["run_id"], "ai")
    assert ai.get("reused_from_research") is True


def test_deadline_degrades_instead_of_blocking(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    v2cfg.v2.research.stage_deadline_sec = 0
    pipe, _ = _pipe(v2cfg, store, bars, make_docs(["AAPL", "MSFT"], d), monkeypatch=monkeypatch)
    rr = pipe.research(as_of=d)
    v2 = store.stage_artifact(rr["run_id"], "v2_research")
    assert v2["deadline_hit"] and "extraction_deadline" in v2["degraded"]
    assert rr["status"] == "ok"


def test_book_aware_risk_sector_and_dust_in_report(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, _ = _pipe(v2cfg, store, bars, [], monkeypatch=monkeypatch)
    st = store.kv_get("sim_state:sim") or {"cash": 1990.0, "positions": {}, "orders": {}, "prices": {}, "last_equity": 2000.0, "fills": [], "date": None}
    st["positions"]["GLD"] = {"qty": 0.001, "avg_price": 100.0}     # dust
    store.kv_set("sim_state:sim", st)
    r = pipe.run(as_of=d)
    book = store.stage_artifact(r["run_id"], "risk")["stats"]["book"]
    assert book["dust"] == ["GLD"] and "sector_actual" in book and "scenarios" in store.stage_artifact(r["run_id"], "risk")["stats"]


def test_morning_delta_is_focused_and_capped(v2cfg, monkeypatch):
    store = Store(v2cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    pipe, prov = _pipe(v2cfg, store, bars, make_docs(["AAPL"], d), monkeypatch=monkeypatch)
    assert pipe.research(as_of=d)["status"] == "ok"
    n0 = len(prov.calls)
    src = pipe.doc_sources()[0]
    late_ts = (datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23)).isoformat()
    titles = ["AAPL raises full-year guidance", "AAPL announces a new buyback program", "AAPL faces an antitrust probe in Europe",
              "MSFT wins a cloud contract", "NVDA ships new chip", "JPM raises dividend", "XOM expands drilling", "GLD sees inflows", "TLT falls on rates"]
    for i, (s, t) in enumerate(zip(["AAPL", "AAPL", "AAPL", "MSFT", "NVDA", "JPM", "XOM", "GLD", "TLT"], titles)):   # only AAPL is a candidate
        doc = make_docs([s], d, titles={s: t})[0]
        doc.doc_id = f"late_{s}_{i}"; doc.available_at = doc.published_at = doc.retrieved_at = late_ts
        src.docs.append(doc)
    v2cfg.v2.research.morning_delta_max_docs = 2
    r = pipe.run(as_of=d)
    v2 = store.stage_artifact(r["run_id"], "v2_research")
    assert v2["delta"]["n_new_docs_total"] == 9 and v2["delta"]["n_docs"] == 2 and v2["delta"]["focus_symbols"] == ["AAPL"]
    assert len(prov.calls) - n0 <= 2 + 6           # at most 2 extractions plus one candidate's passes
    assert "deadline_exceeded" not in r["summary"]["attention_flags"]
