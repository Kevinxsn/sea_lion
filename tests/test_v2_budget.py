"""Budget exhaustion must be visible: degraded flag, abstain reasons, and arm C marked unavailable."""
from datetime import date

from conftest import FakeDocs, FakeEvents, FakeMarketData, FakeProvider, make_docs, synthetic_bars
from sea_lion import pipeline as P
from sea_lion.ai import router as RT
from sea_lion.calendar import TradingCalendar
from sea_lion.store import Store


def test_budget_exhaustion_is_reported(cfg, monkeypatch):
    cfg.v2.sources.sec_enabled = cfg.v2.sources.fred_enabled = cfg.v2.sources.fundamentals_enabled = False
    cfg.notify.transport = "fake"
    cfg.v2.research.concurrency = 1
    cfg.ai.pricing = {"default": {"input": 1000.0, "output": 1000.0}}
    cfg.ai.budget.daily_usd = 0.16                      # one 150-token fake call (~$0.15) trips the hard stop
    store = Store(cfg.db_path)
    bars = synthetic_bars(n_days=300)
    d = date.fromisoformat(max(bars["date"]))
    prov = FakeProvider()
    monkeypatch.setattr(RT, "build_provider", lambda *a, **k: prov)
    pipe = P.Pipeline(cfg, store, market_data=FakeMarketData(bars), event_source=FakeEvents([]),
                      doc_sources=[FakeDocs(make_docs(["AAPL", "MSFT", "NVDA"], d))], calendar=TradingCalendar(store, provider="fallback"))
    r = pipe.research(as_of=d)
    v2 = store.stage_artifact(r["run_id"], "v2_research")
    assert "budget_exhausted" in v2["degraded"] and v2["available"] is False
    assert all("budget_exhausted" in p["abstain_reason"] for p in v2["forecasts"].values())
