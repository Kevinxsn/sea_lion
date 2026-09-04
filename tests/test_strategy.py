from conftest import synthetic_bars
from sea_lion import features as F, strategy as S
from sea_lion.config import FeatureCfg, RiskCfg, StrategyCfg


def _scored():
    fcfg, scfg = FeatureCfg(), StrategyCfg(signal_weights={"mom_short": .3, "mom_long": .35, "trend": .25, "volume_ratio": .1})
    bars = synthetic_bars(n_days=200)
    fp = F.compute_feature_panel(F.Panel.from_long(bars), fcfg)
    feat = F.features_at(fp, max(bars["date"]))
    return S.quant_scores(feat, fcfg, scfg), scfg


def test_ai_capped_at_20pct_and_confidence_floor():
    scored, scfg = _scored()
    rcfg = RiskCfg()
    sym = scored.index[0]
    strong = S.ensemble(scored, {sym: S.AIScore(1.0, 0.95, [], ["e"])}, scfg, rcfg)
    weak = S.ensemble(scored, {sym: S.AIScore(1.0, 0.30, [], ["e"])}, scfg, rcfg)
    none = S.ensemble(scored, {}, scfg, rcfg)
    assert abs(strong.loc[sym, "ensemble_score"] - none.loc[sym, "ensemble_score"] - 0.20) < 1e-9
    assert weak.loc[sym, "ensemble_score"] == none.loc[sym, "ensemble_score"]
    assert (strong["quant_only_score"] == none["quant_only_score"]).all()


def test_weights_respect_gross_and_single_cap():
    scored, scfg = _scored()
    rcfg = RiskCfg()
    scored = S.ensemble(scored, {}, scfg, rcfg)
    w, qw, cands, _ = S.build_proposal(scored, {"multiplier": 1.0}, scfg, rcfg)
    assert len(w) <= scfg.max_positions
    assert sum(w.values()) <= rcfg.gross_exposure_max + 1e-9
    assert all(v <= rcfg.single_position_max + 1e-9 for v in w.values())
    w2, *_ = S.build_proposal(scored, {"multiplier": 0.3}, scfg, rcfg)
    assert sum(w2.values()) <= 0.3 * rcfg.gross_exposure_max + 1e-9
    assert w == qw   # no AI => identical to quant-only


def test_no_candidates_means_cash():
    scored, scfg = _scored()
    scored["eligible"] = False
    scored = S.ensemble(scored, {}, scfg, RiskCfg())
    w, *_ = S.build_proposal(scored, {"multiplier": 1.0}, scfg, RiskCfg())
    assert w == {}
