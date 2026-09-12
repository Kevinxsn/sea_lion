"""Regression: the real model omitted the `text` key and wrote long/odd values; parsing must be lenient
on surface form but strict on evidence. Also: macro observations retrieved after the session are usable."""
from sea_lion.ai.v2_schemas import Claim, ExtractionOutput
from sea_lion.features import macro_features


def test_claim_aliases_truncation_and_enum_coercion():
    raw = {"claim": "x" * 500, "evidence": "the quote", "ticker": "aapl", "event_type": "Merger", "claim_time": "y" * 100,
           "certainty": 1.7, "quantity": 12}
    c = Claim(**raw)
    assert c.text == "x" * 400 and c.evidence_span == "the quote" and c.symbols == ["AAPL"]
    assert c.event_type == "m_and_a" and len(c.claim_time) == 80 and c.certainty == 1.0 and c.quantity == "12"
    out = ExtractionOutput(doc_relevance=2, doc_event_type="Weird Type", doc_sentiment=-3, doc_importance="0.4", one_line="z" * 400,
                           claims=[raw, "not a dict", {"text": "ok", "evidence_span": "e"}])
    assert out.doc_relevance == 1.0 and out.doc_event_type == "other" and out.doc_sentiment == -1.0 and out.doc_importance == 0.4
    assert len(out.claims) == 2 and len(out.one_line) == 300


def test_macro_features_use_run_cutoff(store):
    store.upsert_macro([{"series": "DGS10", "effective_date": "2026-09-10", "value": 4.8, "retrieved_at": "2026-09-11T18:00:00+00:00",
                         "available_at": "2026-09-11T18:00:00+00:00"}])
    assert macro_features(store, "2026-09-10", "2026-09-11T19:00:00+00:00")["dgs10"] == 4.8
    assert macro_features(store, "2026-09-10", "2026-09-10T23:59:59+00:00")["dgs10"] is None     # not yet retrieved then
    assert macro_features(store, "2026-09-09", "2026-09-11T19:00:00+00:00")["dgs10"] is None     # effective after as_of


def test_context_output_negative_magnitude_and_up_down():
    from sea_lion.ai.v2_schemas import ContextOutput
    o = ContextOutput(sector_channel="x", second_order=[{"symbol": "CAT", "direction": "negative", "magnitude": -0.1},
                                                         {"symbol": "CVX", "direction": "up", "magnitude": 0.5}], adjustment_bps={"5d": 500}, macro_flags=[])
    assert o.second_order[0].direction == "negative" and o.second_order[0].magnitude == 0.1
    assert o.second_order[1].direction == "positive" and o.adjustment_bps["5d"] == 100
