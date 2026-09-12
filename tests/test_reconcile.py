"""Regression tests for the 2026-09-09 false safe-mode and the ABBV dust position."""
from sea_lion import execution as X, risk as RK
from sea_lion.broker.base import OPEN_STATUSES
from sea_lion.broker.sim import SimBroker, empty_state
from sea_lion.config import RiskCfg
from sea_lion.reconcile import reconcile


def test_store_open_orders_covers_every_broker_open_status(store):
    for i, st in enumerate(sorted(OPEN_STATUSES)):
        store.upsert_order({"client_order_id": f"c{i}", "run_id": "r", "symbol": "AAPL", "side": "buy", "status": st})
    store.upsert_order({"client_order_id": "done", "run_id": "r", "symbol": "AAPL", "side": "buy", "status": "filled"})
    assert {o["status"] for o in store.open_orders()} == OPEN_STATUSES


def test_pending_new_fill_is_counted_by_reconcile(cfg, store):
    """An order recorded as pending_new fills at the broker; reconcile must count it, not flag a mismatch."""
    br = SimBroker(empty_state(2000.0), slippage_bps=0)
    br.mark({"AAPL": 100.0})
    bo = br.submit_limit_order("AAPL", "buy", 1.0, 101.0, "cid-1", reference_price=100.0)
    row = bo.to_row("run-1", 100.0)
    row["status"] = "pending_new"           # what Alpaca returns right after submission
    store.upsert_order(row)
    br.settle("2026-09-09", {"AAPL": 100.0}, {"AAPL": 100.0})   # fills
    res = reconcile(store, br, cfg, "run-2")
    assert res["ok"], res
    assert res["updated"] == 1 and store.order("cid-1")["status"] == "filled"
    assert store.kv_get("expected_positions") == {"AAPL": 1.0}
    assert not store.safe_mode_active()


def test_full_exit_closes_entire_position_without_dust(cfg, store):
    a = RK.AccountState(equity=2000.0, cash=1900.0, positions={"ABBV": 0.182}, prices={"ABBV": 250.0, "X": 10.0},
                        start_of_day_equity=2000.0, high_water_mark=2000.0)
    res = RK.evaluate({}, a, {"ABBV": "Health Care", "X": "Other"}, RiskCfg())
    sell = [i for i in res.intents if i.symbol == "ABBV"][0]
    assert sell.side == "sell" and sell.close_position
    br = SimBroker(empty_state(1900.0)); br.s["positions"]["ABBV"] = {"qty": 0.182, "avg_price": 250.0}; br.mark({"ABBV": 250.0})
    # fresh price higher than the close would previously shrink qty below the position -> dust
    rep = X.execute([sell], br, store, cfg, "run-x", {"ABBV": 251.0}, True)
    assert rep.submitted[0]["qty"] == 0.182


def test_partial_reduction_keeps_rounding_path():
    a = RK.AccountState(equity=2000.0, cash=0.0, positions={"A": 10.0}, prices={"A": 100.0},
                        start_of_day_equity=2000.0, high_water_mark=2000.0)
    res = RK.evaluate({"A": 0.10}, a, {"A": "Tech"}, RiskCfg())   # 50% -> 10%: a partial sell, not a close
    i = res.intents[0]
    assert i.side == "sell" and not i.close_position
