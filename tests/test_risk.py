import pytest

from sea_lion import risk as RK
from sea_lion.config import RiskCfg

SEC = {"A": "Tech", "B": "Tech", "C": "Tech", "D": "Fin", "E": "Fin", "F": "Energy", "G": "Health"}
PX = {s: 100.0 for s in SEC}


def acct(**kw):
    base = dict(equity=2000.0, cash=2000.0, positions={}, prices=PX, start_of_day_equity=2000.0, high_water_mark=2000.0)
    base.update(kw)
    return RK.AccountState(**base)


def reasons(res, sym):
    return [r for d in res.decisions if d.symbol == sym for r in d.reasons]


def test_single_position_clamp():
    res = RK.evaluate({"A": 0.30}, acct(), SEC, RiskCfg())
    assert res.approved["A"] == pytest.approx(0.10)
    assert any(r.startswith("clamp_single") for r in reasons(res, "A"))


def test_gross_exposure_cap():
    prop = {s: 0.10 for s in SEC}   # 70% but sector caps apply first
    cfg = RiskCfg(sector_max=1.0, gross_exposure_max=0.5)
    res = RK.evaluate(prop, acct(), SEC, cfg)
    assert sum(res.approved.values()) <= 0.5 + 1e-5
    assert all(any(r.startswith("gross_scaled") for r in reasons(res, s)) for s in res.approved)


def test_max_positions():
    cfg = RiskCfg(max_positions=3, sector_max=1.0)
    res = RK.evaluate({s: 0.05 for s in SEC}, acct(), SEC, cfg, rank_order=list(SEC))
    assert set(res.approved) == {"A", "B", "C"}
    assert "max_positions_3" in reasons(res, "D")


def test_sector_cap_reduces_lowest_ranked():
    res = RK.evaluate({"A": 0.10, "B": 0.10, "C": 0.10}, acct(), SEC, RiskCfg(), rank_order=["A", "B", "C"])
    assert res.approved["A"] == 0.10 and res.approved["B"] == 0.10
    assert res.approved["C"] == pytest.approx(0.05)
    assert any(r.startswith("sector_cap_Tech") for r in reasons(res, "C"))


def test_daily_loss_lock_blocks_buys_allows_sells():
    a = acct(equity=1950.0, cash=950.0, positions={"A": 10.0}, start_of_day_equity=2000.0)  # -2.5%
    res = RK.evaluate({"A": 0.0, "B": 0.10}, a, SEC, RiskCfg())
    assert res.block_new_exposure
    assert any(f.startswith("daily_loss_lock") for f in res.global_flags)
    sides = {i.symbol: i.side for i in res.intents}
    assert sides == {"A": "sell"}
    assert "buy_blocked" in reasons(res, "B") or "new_exposure_blocked" in reasons(res, "B")


def test_drawdown_enters_safe_mode_without_liquidation():
    a = acct(equity=1780.0, cash=780.0, positions={"A": 10.0}, high_water_mark=2000.0, start_of_day_equity=1780.0)
    res = RK.evaluate({"A": 0.10, "B": 0.10}, a, SEC, RiskCfg())
    assert res.enter_safe_mode
    assert res.block_new_exposure
    assert res.intents == []                                   # nothing auto-submitted in safe mode
    assert [i.symbol for i in res.held_for_review] == ["A"]    # the risk-reducing sell waits for a human
    assert "sells_held_for_manual_review" in res.global_flags


def test_stale_data_blocks_all_orders():
    a = acct(data_stale=True, cash=1000.0, positions={"F": 10.0})
    res = RK.evaluate({"A": 0.10, "F": 0.0}, a, SEC, RiskCfg())
    assert res.intents == [] and "data_stale" in res.global_flags and "all_orders_blocked" in res.global_flags


def test_safe_mode_blocks_new_exposure():
    res = RK.evaluate({"A": 0.10}, acct(safe_mode=True), SEC, RiskCfg())
    assert res.intents == []


def test_turnover_cap_scales_buys_keeps_sells():
    cfg = RiskCfg(sector_max=1.0, turnover_max=0.20)
    a = acct(cash=1000.0, positions={"F": 10.0})  # F worth 1000 -> equity 2000
    res = RK.evaluate({"A": 0.10, "B": 0.10, "C": 0.10, "D": 0.10, "E": 0.10}, a, SEC, cfg)
    sells = [i for i in res.intents if i.side == "sell"]
    assert sells and sells[0].symbol == "F" and sells[0].notional == pytest.approx(1000.0)
    buys = sum(i.notional for i in res.intents if i.side == "buy")
    assert buys == 0.0                      # sell alone already exceeds the cap; buys are scaled to zero
    assert any(f.startswith("turnover_capped") for f in res.global_flags)
    cfg2 = RiskCfg(sector_max=1.0, turnover_max=0.60)
    res2 = RK.evaluate({"A": 0.10, "B": 0.10, "C": 0.10, "D": 0.10, "E": 0.10}, a, SEC, cfg2)
    buys2 = sum(i.notional for i in res2.intents if i.side == "buy")
    assert buys2 == pytest.approx(0.60 * 2000 - 1000.0)
    assert res2.intents[0].side == "sell"   # sells sorted first


def test_min_and_max_order_size():
    cfg = RiskCfg(sector_max=1.0, turnover_max=1.0)
    a = acct(equity=2000.0, cash=2000.0)
    res = RK.evaluate({"A": 0.004}, a, SEC, cfg)     # $8 < $10 minimum
    assert res.intents == []
    a2 = acct(equity=20000.0, cash=20000.0)
    res2 = RK.evaluate({"A": 0.10}, a2, SEC, cfg)
    assert res2.intents[0].notional <= 0.10 * 20000 + 1e-6


def test_short_and_unknown_rejected():
    res = RK.evaluate({"A": -0.1, "ZZZ": 0.1}, acct(), SEC, RiskCfg())
    assert res.approved == {}
    assert "short_not_allowed" in reasons(res, "A")
    assert "symbol_not_in_universe" in reasons(res, "ZZZ")


def test_buying_power_limits_buys():
    cfg = RiskCfg(sector_max=1.0, turnover_max=5.0)
    a = acct(equity=230.0, cash=30.0, positions={}, start_of_day_equity=230.0, high_water_mark=230.0)
    res = RK.evaluate({"A": 0.10, "D": 0.10}, a, SEC, cfg)          # wants $46 of buys with $30 cash
    buys = sum(i.notional for i in res.intents if i.side == "buy")
    assert buys == pytest.approx(30.0)
    assert "buying_power_limited" in res.global_flags


def test_presubmit_checks():
    it = RK.OrderIntent("A", "buy", 100.0, 100.0)
    cfg = RiskCfg()
    assert RK.presubmit_check(it, 100.0, 1000.0, 0.0, 2000.0, cfg, True) is None
    assert RK.presubmit_check(it, 100.0, 1000.0, 0.0, 2000.0, cfg, False) == "market_closed"
    assert RK.presubmit_check(it, 110.0, 1000.0, 0.0, 2000.0, cfg, True).startswith("price_moved")
    assert RK.presubmit_check(it, 100.0, 50.0, 0.0, 2000.0, cfg, True) == "insufficient_buying_power"
    assert RK.presubmit_check(RK.OrderIntent("A", "sell", 100.0, 100.0), 100.0, 0.0, 0.5, 2000.0, cfg, True) == "sell_exceeds_position"
    assert RK.presubmit_check(RK.OrderIntent("A", "buy", 5.0, 100.0), 100.0, 1000.0, 0.0, 2000.0, cfg, True) == "below_min_notional"
