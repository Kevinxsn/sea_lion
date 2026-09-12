"""Unit tests for V2 building blocks: calendar, notify, documents, events, portfolio, forecasts."""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from conftest import synthetic_bars
from sea_lion import forecasts as FC, notify as N, portfolio as PF
from sea_lion.calendar import ET, TradingCalendar
from sea_lion.config import HysteresisCfg, NotifyCfg, PortfolioRiskCfg
from sea_lion.data.documents import DocumentStore, SourceDocument, fingerprint, near_duplicate
from sea_lion.events.cluster import EventClusterer
from sea_lion.events.entities import EntityRegistry
from sea_lion.events.routing import route, select_candidates


# ---------------------------------------------------------------- calendar
def test_calendar_fallback_sessions_and_completed_session(store):
    cal = TradingCalendar(store, provider="fallback")
    assert cal.source == "fallback"
    assert not cal.is_session("2026-09-07")           # Labor Day
    assert cal.is_session("2026-09-08")
    assert cal.previous_session("2026-09-07").date == "2026-09-04"
    assert cal.nth_session_after("2026-09-04", 1) == "2026-09-08"
    # 10:00 ET on a session -> last completed is the previous session
    now = datetime(2026, 9, 9, 10, 0, tzinfo=ET)
    assert cal.last_completed_session(now) == "2026-09-08"
    assert cal.last_completed_session(datetime(2026, 9, 9, 16, 30, tzinfo=ET)) == "2026-09-09"
    assert cal.in_decision_window(["09:35", "15:30"], now)
    assert not cal.in_decision_window(["09:35", "15:30"], datetime(2026, 9, 9, 9, 20, tzinfo=ET))
    assert not cal.in_decision_window(["09:35", "15:30"], datetime(2026, 9, 7, 10, 0, tzinfo=ET))
    st = cal.market_state(datetime(2026, 11, 27, 12, 0, tzinfo=ET))
    assert st["early_close"] and st["is_open"]


def test_calendar_uses_cache_when_fresh(store):
    store.kv_set("calendar:sessions", {"2026-09-08": {"open": "09:30", "close": "16:00"}})
    store.kv_set("calendar:meta", {"fetched_on": date.today().isoformat(), "end": (date.today() + timedelta(days=60)).isoformat(), "source": "alpaca"})
    cal = TradingCalendar(store, provider="alpaca", api_key="x", secret_key="y")
    assert cal.source == "alpaca" and cal.is_session("2026-09-08") and not cal.is_session("2026-09-09")


# ---------------------------------------------------------------- notify
def test_notify_persists_and_sends_fake(store):
    N.SENT.clear()
    n = N.Notifier(NotifyCfg(transport="fake", email_to=["a@example.com", "b@example.com"]), store, "paper")
    hid = n.event("critical", "reconcile", "position mismatch", run_id="r1", remediation="clear safe mode")
    assert len(N.SENT) == 2 and "CRITICAL" in N.SENT[0]["subject"]
    att = store.q("SELECT * FROM notification_attempts WHERE health_event_id=?", (hid,))
    assert len(att) == 2 and all(a["status"] == "sent" for a in att)
    n.event("info", "pipeline", "uneventful")
    assert len(N.SENT) == 2                      # info never emails
    assert len(store.health_events_for_run("r1")) == 1


def test_notify_failure_is_audited_not_raised(store):
    n = N.Notifier(NotifyCfg(transport="smtp", smtp_host="127.0.0.1", smtp_port=1, email_to=["x@example.com"], max_retries=1), store, "paper")
    hid = n.event("warning", "ai", "provider down")
    att = store.q("SELECT status FROM notification_attempts WHERE health_event_id=?", (hid,))
    assert len(att) == 2 and all(a["status"] == "failed" for a in att)


def test_summary_line_format():
    line = N.summary_line({"run_date": "2026-09-11", "run_outcome": "completed", "safe_mode": False, "n_orders": 3})
    assert line.startswith("SEA_LION_SUMMARY run_date=2026-09-11") and "n_orders=3" in line


# ---------------------------------------------------------------- documents
def _doc(i, title, syms=("AAPL",), published=None, source="alpaca_benzinga", body="", retrieved=None):
    now = datetime.now(timezone.utc)
    pub = (published or now - timedelta(hours=1)).isoformat()
    return SourceDocument(doc_id=f"d{i}", source=source, doc_type="news", symbols=list(syms), title=title, summary="s",
                          content=body, published_at=pub, retrieved_at=(retrieved or now).isoformat(), available_at=pub,
                          source_quality=0.7)


def test_document_quarantine_and_dedup(store, tmp_path):
    ds = DocumentStore(store, tmp_path / "docs")
    uni = {"AAPL", "MSFT"}
    future = _doc(1, "Apple beats", published=datetime.now(timezone.utc) + timedelta(hours=2))
    st = ds.ingest([future], "r", uni)
    assert st["quarantined"] == 1 and store.doc_exists("d1")["quarantined"] == 1
    a = _doc(2, "Apple beats estimates on iPhone demand", body="long body")
    b = _doc(3, "Apple beats estimates on iPhone demand", body="long body")       # exact dup (different id)
    c = _doc(4, "On iPhone demand, Apple beats estimates")                       # near dup (same fingerprint)
    d = _doc(5, "Microsoft launches new Azure region", syms=("MSFT",))
    e = _doc(6, "Unrelated ticker news", syms=("ZZZ",))
    st = ds.ingest([a, b, c, d, e], "r", uni)
    assert st["new"] == 3 and st["exact_dup"] == 1 and st["near_dup"] == 1
    assert store.doc_exists("d4")["source_quality"] <= 0.4
    docs = store.documents("2000", "9999", symbols=["AAPL"])
    assert {x["doc_id"] for x in docs} == {"d2", "d4"}
    assert ds.read_body(store.doc_exists("d2")["content_ref"]) == "long body"
    # revision: same id, changed content -> revision row, not overwrite-silently
    a2 = _doc(2, "Apple beats estimates on iPhone demand (updated)", body="longer body")
    st = ds.ingest([a2], "r2", uni)
    assert st["revised"] == 1 and store.q1("SELECT COUNT(*) n FROM document_revisions")["n"] == 1


def test_fingerprint_and_near_duplicate():
    assert fingerprint("Apple beats estimates") == fingerprint("apple beats the estimates!")
    assert near_duplicate("Merck wins FDA approval for oral PCSK9 drug", "", "FDA approves Merck's oral PCSK9 drug", "")
    assert not near_duplicate("Merck wins FDA approval", "", "Chevron doubles Venezuela rig count", "")


# ---------------------------------------------------------------- entities / clustering / routing
def test_entity_resolution():
    reg = EntityRegistry({"AAPL": "Technology", "MSFT": "Technology", "V": "Financials"},
                         cik_map={"MSFT": {"cik": "0000789019", "name": "MICROSOFT CORP"}})
    found = {m[0]: m[3] for m in reg.resolve("Apple and $MSFT rally; Visa Inc. also up. V is a letter.")}
    assert found["AAPL"] == "alias" and found["MSFT"] == "ticker" and found["V"] == "alias"
    assert reg.cik("MSFT") == "0000789019" and reg.sector_etf("V") == "XLF"


def test_clusterer_merges_same_story_and_scores_novelty(store):
    cl = EventClusterer(store)
    t = "2026-09-08T13:00:00+00:00"
    e1, o1 = cl.assign("MRK", "regulatory", "Merck wins FDA approval for oral PCSK9 inhibitor", "", t, "docA", ["c1"], 0.8, 0.5, 0.7, "r1")
    e2, o2 = cl.assign("MRK", "regulatory", "FDA approves Merck's oral PCSK9 inhibitor", "", "2026-09-08T15:00:00+00:00", "docB", ["c2"], 0.6, 0.4, 0.5, "r1")
    assert o1 == "new" and o2 == "merged" and e2["event_id"] == e1["event_id"]
    assert set(e2["document_ids"]) == {"docA", "docB"} and e2["importance"] == 0.8
    e3, o3 = cl.assign("MRK", "product", "Merck opens new manufacturing plant in Ireland", "", "2026-09-09T13:00:00+00:00", "docC", [], 0.3, 0.1, 0.7, "r2")
    assert o3 == "new" and e3["novelty"] > 0.5
    assert len(store.events("2026-09-01", "2026-09-30")) == 2


def test_routing_policy():
    base = {"event_type": "macro", "importance": 0.5, "source_quality": 0.7, "novelty": 0.9}
    assert route(base, is_filing=False, is_rumor=False, corroborated=False, macro_gate=0.7, analyst_requires_corroboration=True)[1] is False
    assert route(dict(base, importance=0.75), is_filing=False, is_rumor=False, corroborated=False, macro_gate=0.7, analyst_requires_corroboration=True)[1] is True
    an = dict(base, event_type="analyst", importance=0.6)
    assert route(an, is_filing=False, is_rumor=False, corroborated=False, macro_gate=0.7, analyst_requires_corroboration=True)[2] == "analyst_uncorroborated"
    assert route(an, is_filing=False, is_rumor=False, corroborated=True, macro_gate=0.7, analyst_requires_corroboration=True)[1] is True
    fil = {"event_type": "earnings", "importance": 0.85, "source_quality": 0.95, "novelty": 0.9}
    assert route(fil, is_filing=True, is_rumor=False, corroborated=False, macro_gate=0.7, analyst_requires_corroboration=True)[0] == "filing_material"
    assert route(dict(fil, source_quality=0.2), is_filing=False, is_rumor=False, corroborated=True, macro_gate=0.7, analyst_requires_corroboration=True)[0] == "low_quality"
    assert route(fil, is_filing=True, is_rumor=True, corroborated=True, macro_gate=0.7, analyst_requires_corroboration=True)[0] == "rumor"
    assert route(dict(fil, novelty=0.05), is_filing=True, is_rumor=False, corroborated=True, macro_gate=0.7, analyst_requires_corroboration=True)[0] == "duplicate"


def test_candidate_selection_reserves_filing_slots():
    ev = lambda cls, imp: {"actionable": True, "routing_class": cls, "importance": imp}  # noqa: E731
    by = {"AAA": [ev("macro", 0.9)], "BBB": [ev("company_news", 0.5)], "CCC": [ev("filing_material", 0.85)], "DDD": [ev("filing_material", 0.7)],
          "EEE": [ev("analyst", 0.3)]}
    cands, why = select_candidates(by, ["AAA", "BBB", "EEE", "DDD", "CCC"], main_candidates=3, reserve_for_filings=2, material_importance=0.5)
    assert cands[:2] == ["CCC", "DDD"] and why["CCC"] == "material_filing" and len(cands) == 3


# ---------------------------------------------------------------- portfolio
def _scored(scores):
    df = pd.DataFrame({"ensemble_score": scores, "eligible": True, "above_trend": 1.0, "vol": 0.2})
    return df


def test_hysteresis_retains_and_blocks_churn():
    syms = [f"S{i:02d}" for i in range(20)]
    sc = _scored(pd.Series(np.linspace(1.0, 0.62, 20), index=syms))   # step 0.02
    cfg = HysteresisCfg(enabled=True, entry_rank=10, retention_rank=15, score_band=0.05, replacement_margin=0.08, round_trip_cost=0.001)
    # holding S12 (rank 13): retained by rank; a top-10 entrant that only marginally beats it is blocked
    sel, why = PF.select_with_hysteresis(sc, ["S12"] + syms[:9], cfg, 10)
    assert "S12" in sel and why["S12"] == "retained_rank_13" and why["S09"] == "blocked_by_hysteresis"
    # holding S18 (rank 19, far below band): dropped
    sel2, why2 = PF.select_with_hysteresis(sc, ["S18"], cfg, 10)
    assert "S18" not in sel2 and why2["S18"] == "dropped_rank_19" and len(sel2) == 10
    # a much stronger entrant replaces the weakest retained holding
    sc3 = sc.copy(); sc3.loc["S00", "ensemble_score"] = 5.0
    sel3, why3 = PF.select_with_hysteresis(sc3, ["S12"] + syms[1:10], cfg, 10)
    assert "S00" in sel3 and why3["S12"].startswith("replaced_by_")


def test_clusters_beta_scenarios():
    bars = synthetic_bars(n_days=120)
    from sea_lion.features import Panel
    ac = Panel.from_long(bars).adj_close
    ac["AAPL2"] = ac["AAPL"] * 1.001           # perfectly correlated twin
    cl = PF.correlation_clusters(ac, max(ac.index), 60, 0.8)
    assert cl["AAPL"] == cl["AAPL2"]
    w = {"AAPL": 0.1, "AAPL2": 0.1, "XOM": 0.1}
    assert PF.cluster_exposure(w, cl)[cl["AAPL"]] == pytest.approx(0.2)
    assert PF.portfolio_beta(w, {"AAPL": 1.5, "AAPL2": 1.5, "XOM": 0.5}) == pytest.approx(0.35)
    sc = PF.scenario_checks(w, {"AAPL": 1.5, "AAPL2": 1.5, "XOM": 0.5}, {"AAPL": "Technology", "AAPL2": "Technology", "XOM": "Energy"}, cl,
                            {"AAPL": 0.01}, PortfolioRiskCfg())
    assert sc["market_-3pct"] == pytest.approx(-0.03 * 0.35) and sc["tech_-5pct"] == pytest.approx(-0.01) and sc["largest_gap_-10pct"] == pytest.approx(-0.01)


# ---------------------------------------------------------------- forecasts / calibration
def test_overlay_and_shrink_and_outcome_scoring(store):
    hz = {"5d": {"p_positive_excess_return": 0.8, "expected_excess_return_bps": 100}, "10d": {"p_positive_excess_return": 0.7, "expected_excess_return_bps": 100},
          "20d": {"p_positive_excess_return": 0.6, "expected_excess_return_bps": 100}}
    cal, mode = FC.calibrate_forecast(store, hz, 0.5)
    assert mode == "shrunk" and cal["5d"]["p_positive_excess_return"] == pytest.approx(0.65)
    ov, info = FC.overlay_score(cal, 0.8, 1.0, False, 0.4)
    assert 0 < ov <= 1 and info["reason"] == "ok"
    assert FC.overlay_score(cal, 0.3, 1.0, False, 0.4)[0] == 0.0
    assert FC.overlay_score(cal, 0.9, 1.0, True, 0.4)[0] == 0.0
    # freeze + score
    sessions = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]
    ac = pd.DataFrame({"AAPL": [100, 101, 102, 103, 104, 106, 108], "SPY": [100, 100, 100, 100, 100, 100, 100]}, index=sessions)
    FC.freeze(store, "r1", "C", "2026-09-01", "t", "AAPL", hz, hz, cal, 0.8, 0.1, False, ["c1"], [], ov, 100.0, 100.0, "v2.0")
    res = FC.score_outcomes(store, sessions, ac, "SPY", [5, 10, 20])
    assert res["scored"] == 1 and res["pending"] == 2
    o = store.scored_outcomes(5)[0]
    assert o["hit"] == 1 and o["excess_return"] == pytest.approx(0.06) and o["p_positive"] == pytest.approx(0.65)
    assert FC.calibration_stats(store.scored_outcomes(5))["n"] == 1
    assert FC.maybe_fit(store, 5, 60, "v2.0") is None


def test_logistic_fit_recovers_overconfidence():
    rng = np.random.default_rng(0)
    ps = rng.uniform(0.3, 0.9, 400)
    true_p = 0.5 + (ps - 0.5) * 0.4                    # model is overconfident by 2.5x
    ys = (rng.uniform(size=400) < true_p).astype(int)
    params = FC.fit_logistic(list(ps), list(ys))
    assert 0.2 < params["b"] < 0.7
    assert abs(FC.apply_calibration(0.9, params) - 0.66) < 0.08
