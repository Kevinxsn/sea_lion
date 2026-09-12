from sea_lion.events.cluster import EventClusterer


def test_filing_and_news_about_same_earnings_merge(store):
    cl = EventClusterer(store)
    e1, o1 = cl.assign("ORCL", "earnings", "ORCL 8-K items 2.02, 9.01 filed 2026-09-10", "", "2026-09-10T20:05:00+00:00", "sec1", ["c1"], 0.85, 0.0, 0.95, "r")
    e2, o2 = cl.assign("ORCL", "earnings", "Oracle beats on cloud growth, raises guidance", "", "2026-09-10T21:30:00+00:00", "news1", ["c2"], 0.8, 0.6, 0.7, "r")
    assert o2 == "merged" and e2["event_id"] == e1["event_id"] and e2["source_quality"] == 0.95 and set(e2["document_ids"]) == {"sec1", "news1"}
    e3, o3 = cl.assign("ORCL", "product", "Oracle launches new database version", "", "2026-09-10T22:00:00+00:00", "news2", [], 0.4, 0.2, 0.7, "r")
    assert o3 == "new"
