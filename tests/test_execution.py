import pytest

from sea_lion import execution as X
from sea_lion.broker.base import AmbiguousBrokerResponse, BrokerError
from sea_lion.broker.sim import SimBroker, empty_state
from sea_lion.risk import OrderIntent


def test_client_order_id_deterministic():
    a = X.client_order_id("run1", "AAPL", "buy", "v1.0")
    assert a == X.client_order_id("run1", "AAPL", "buy", "v1.0")
    assert a != X.client_order_id("run1", "AAPL", "sell", "v1.0")
    assert a != X.client_order_id("run2", "AAPL", "buy", "v1.0")
    assert a.startswith("sl-") and len(a) < 64


def test_limit_price_offsets():
    assert X.limit_price(100.0, "buy", 15) == pytest.approx(100.15)
    assert X.limit_price(100.0, "sell", 15) == pytest.approx(99.85)


def test_duplicate_submission_creates_one_order(cfg, store):
    br = SimBroker(empty_state(2000.0))
    br.mark({"AAPL": 100.0})
    intents = [OrderIntent("AAPL", "buy", 150.0, 100.0)]
    r1 = X.execute(intents, br, store, cfg, "run-x", {"AAPL": 100.0}, True)
    r2 = X.execute(intents, br, store, cfg, "run-x", {"AAPL": 100.0}, True)
    assert len(r1.submitted) == 1 and len(r2.submitted) == 0
    assert r2.duplicates_prevented == 1
    assert len(br.s["orders"]) == 1
    # local record wiped but broker still has it -> still no duplicate
    store.x("DELETE FROM orders")
    r3 = X.execute(intents, br, store, cfg, "run-x", {"AAPL": 100.0}, True)
    assert len(r3.submitted) == 0 and r3.duplicates_prevented == 1 and len(br.s["orders"]) == 1


def test_presubmit_rejection_recorded(cfg, store):
    br = SimBroker(empty_state(2000.0))
    br.mark({"AAPL": 100.0})
    r = X.execute([OrderIntent("AAPL", "buy", 150.0, 100.0)], br, store, cfg, "run-y", {"AAPL": 120.0}, True)
    assert not r.submitted and r.skipped[0]["why"].startswith("price_moved")
    assert store.order(X.client_order_id("run-y", "AAPL", "buy", cfg.run.strategy_version))["status"] == "rejected_local"
    assert br.s["orders"] == {}


class AmbiguousBroker(SimBroker):
    def submit_limit_order(self, *a, **k):
        raise AmbiguousBrokerResponse("timeout")


class RejectingBroker(SimBroker):
    def submit_limit_order(self, *a, **k):
        raise BrokerError("422 insufficient qty")


def test_ambiguous_and_rejected_paths(cfg, store):
    br = AmbiguousBroker(empty_state(2000.0)); br.mark({"AAPL": 100.0})
    r = X.execute([OrderIntent("AAPL", "buy", 150.0, 100.0)], br, store, cfg, "run-a", {"AAPL": 100.0}, True)
    assert len(r.ambiguous) == 1 and store.q1("SELECT status FROM orders")["status"] == "ambiguous"
    br2 = RejectingBroker(empty_state(2000.0)); br2.mark({"AAPL": 100.0})
    r2 = X.execute([OrderIntent("AAPL", "buy", 150.0, 100.0)], br2, store, cfg, "run-b", {"AAPL": 100.0}, True)
    assert len(r2.errors) == 1


def test_kill_switch_cancels_and_enters_safe_mode(cfg, store):
    br = SimBroker(empty_state(2000.0)); br.mark({"AAPL": 100.0})
    X.execute([OrderIntent("AAPL", "buy", 150.0, 100.0)], br, store, cfg, "run-k", {"AAPL": 100.0}, True)
    assert len(br.open_orders()) == 1
    out = X.kill_switch(br, store, "test")
    assert out["canceled"] == 1 and store.safe_mode_active()
    assert br.open_orders() == [] and store.open_orders() == []


def test_sim_broker_fills_and_expires():
    br = SimBroker(empty_state(1000.0), slippage_bps=0)
    br.submit_limit_order("AAPL", "buy", 5, 101.0, "c1", reference_price=100.0)   # fills at the open
    br.submit_limit_order("MSFT", "buy", 1, 99.0, "c2", reference_price=100.0)    # gaps +8% -> expires
    fills = br.settle("2026-01-02", {"AAPL": 100.0, "MSFT": 108.0}, {"AAPL": 102.0, "MSFT": 108.0})
    assert [f.symbol for f in fills] == ["AAPL"]
    assert br.get_order_by_client_id("c2").status == "expired"
    assert br.s["cash"] == pytest.approx(500.0) and br.equity() == pytest.approx(1010.0)
    br.submit_limit_order("AAPL", "sell", 10, 90.0, "c3")     # sell more than held -> capped at position
    br.settle("2026-01-03", {"AAPL": 100.0}, {"AAPL": 100.0})
    assert "AAPL" not in br.s["positions"] and br.s["cash"] == pytest.approx(1000.0)
