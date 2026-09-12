"""Free daily bars + headlines via yfinance. No API key. Good enough for research/shadow/sim;
Alpaca's own data feed is used automatically when paper keys are present (see alpaca_source.py)."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd

from .base import BAR_COLUMNS, Event

log = logging.getLogger(__name__)


class YFinanceData:
    name = "yfinance"

    def fetch_bars(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        import yfinance as yf
        # end is exclusive in yfinance -> add a day
        raw = yf.download(symbols, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                          auto_adjust=False, group_by="ticker", threads=True, progress=False)
        frames = []
        if raw is None or raw.empty:
            return pd.DataFrame(columns=BAR_COLUMNS)
        multi = isinstance(raw.columns, pd.MultiIndex)
        for sym in symbols:
            try:
                sub = raw[sym] if multi else raw
            except KeyError:
                log.warning("yfinance returned no data for %s", sym)
                continue
            sub = sub.dropna(subset=["Close"]).copy()
            if sub.empty:
                continue
            f = pd.DataFrame({
                "symbol": sym,
                "date": [d.date().isoformat() for d in sub.index],
                "open": sub["Open"].astype(float).values,
                "high": sub["High"].astype(float).values,
                "low": sub["Low"].astype(float).values,
                "close": sub["Close"].astype(float).values,
                "adj_close": sub["Adj Close"].astype(float).values if "Adj Close" in sub else sub["Close"].astype(float).values,
                "volume": sub["Volume"].astype(float).values,
            })
            frames.append(f)
        if not frames:
            return pd.DataFrame(columns=BAR_COLUMNS)
        return pd.concat(frames, ignore_index=True)[BAR_COLUMNS]

    def latest_prices(self, symbols: List[str]) -> Dict[str, float]:
        import yfinance as yf
        out: Dict[str, float] = {}
        raw = yf.download(symbols, period="5d", auto_adjust=False, group_by="ticker", threads=True, progress=False)
        multi = isinstance(raw.columns, pd.MultiIndex)
        for sym in symbols:
            try:
                sub = raw[sym] if multi else raw
                close = sub["Close"].dropna()
                if len(close):
                    out[sym] = float(close.iloc[-1])
            except Exception:  # noqa: BLE001
                continue
        return out


class YFinanceNews:
    name = "yfinance_news"

    def fetch_events(self, symbols: List[str], since: datetime, max_per_symbol: int) -> List[Event]:
        import yfinance as yf
        events: List[Event] = []
        for sym in symbols:
            try:
                items = yf.Ticker(sym).news or []
            except Exception as e:  # noqa: BLE001
                log.warning("news fetch failed for %s: %s", sym, e)
                continue
            n = 0
            for it in items:
                ev = _parse_item(sym, it)
                if ev is None:
                    continue
                try:
                    ts = datetime.fromisoformat(ev.published_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < since:
                    continue
                events.append(ev)
                n += 1
                if n >= max_per_symbol:
                    break
        return events


def _parse_item(sym: str, it: dict) -> Event | None:
    """yfinance has shipped two news shapes; handle both defensively."""
    c = it.get("content") if isinstance(it.get("content"), dict) else it
    title = (c.get("title") or "").strip()
    if not title:
        return None
    summary = (c.get("summary") or c.get("description") or "").strip()
    pub = c.get("pubDate") or c.get("providerPublishTime") or c.get("published_at")
    if isinstance(pub, (int, float)):
        pub = datetime.fromtimestamp(pub, tz=timezone.utc).isoformat()
    if not pub:
        return None
    provider = c.get("provider") or {}
    source = provider.get("displayName") if isinstance(provider, dict) else (c.get("publisher") or "")
    url = ""
    cu = c.get("canonicalUrl") or c.get("clickThroughUrl")
    if isinstance(cu, dict):
        url = cu.get("url", "")
    elif isinstance(c.get("link"), str):
        url = c["link"]
    return Event(symbol=sym, published_at=str(pub), title=title[:300], summary=summary[:1200],
                 source=str(source or "yahoo"), url=url)


class YFinanceNewsDocuments:
    """V2 fallback: Yahoo headlines as SourceDocuments (no body; degraded provenance flag)."""
    name = "yahoo"

    def fetch_documents(self, symbols: List[str], since: datetime, limit: int = 10):
        from .documents import LOW_QUALITY_TITLE, SourceDocument, now_utc
        out = []
        for e in YFinanceNews().fetch_events(symbols, since, limit):
            q = 0.30 if LOW_QUALITY_TITLE.search(e.title) else 0.50
            retrieved = now_utc().isoformat(timespec="seconds")
            out.append(SourceDocument(doc_id=f"yahoo_{e.event_id}", source="yahoo", doc_type="news", symbols=[e.symbol],
                                      primary_symbol=e.symbol, title=e.title, summary=e.summary, url=e.url,
                                      published_at=e.published_at, retrieved_at=retrieved, available_at=e.published_at,
                                      event_time=e.published_at, source_quality=q, license_flags="headline_only",
                                      meta={"provenance": "fallback", "source_name": e.source}))
        return out
