"""Alpaca market data + news (free with a paper account). Requires API keys."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List

import pandas as pd

from .base import BAR_COLUMNS, Event

log = logging.getLogger(__name__)


class AlpacaData:
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str):
        from alpaca.data.historical import StockHistoricalDataClient
        self._client = StockHistoricalDataClient(api_key, secret_key)

    def fetch_bars(self, symbols: List[str], start: date, end: date) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=[s.replace("-", ".") for s in symbols], timeframe=TimeFrame.Day,
                               start=datetime.combine(start, datetime.min.time()),
                               end=datetime.combine(end + timedelta(days=1), datetime.min.time()),
                               adjustment=Adjustment.ALL, feed=DataFeed.IEX)
        bars = self._client.get_stock_bars(req)
        df = bars.df.reset_index() if hasattr(bars, "df") else pd.DataFrame()
        if df.empty:
            return pd.DataFrame(columns=BAR_COLUMNS)
        df["symbol"] = df["symbol"].str.replace(".", "-", regex=False)
        df["date"] = pd.to_datetime(df["timestamp"]).dt.date.astype(str)
        # Adjustment.ALL already adjusts OHLC; keep close as the adjusted series too.
        df["adj_close"] = df["close"]
        return df[BAR_COLUMNS].astype({"open": float, "high": float, "low": float, "close": float,
                                       "adj_close": float, "volume": float})

    def latest_prices(self, symbols: List[str]) -> Dict[str, float]:
        from alpaca.data.requests import StockLatestTradeRequest
        req = StockLatestTradeRequest(symbol_or_symbols=[s.replace("-", ".") for s in symbols])
        trades = self._client.get_stock_latest_trade(req)
        return {k.replace(".", "-"): float(v.price) for k, v in trades.items()}


class AlpacaNews:
    name = "alpaca_news"

    def __init__(self, api_key: str, secret_key: str):
        from alpaca.data.historical.news import NewsClient
        self._client = NewsClient(api_key, secret_key)

    def fetch_events(self, symbols: List[str], since: datetime, max_per_symbol: int) -> List[Event]:
        from alpaca.data.requests import NewsRequest
        out: List[Event] = []
        for sym in symbols:
            try:
                req = NewsRequest(symbols=sym.replace("-", "."), start=since, limit=max_per_symbol)
                res = self._client.get_news(req)
                items = res.data.get("news", []) if hasattr(res, "data") else []
            except Exception as e:  # noqa: BLE001
                log.warning("alpaca news failed for %s: %s", sym, e)
                continue
            for n in items:
                out.append(Event(symbol=sym, published_at=str(getattr(n, "created_at", "")),
                                 title=str(getattr(n, "headline", ""))[:300],
                                 summary=str(getattr(n, "summary", ""))[:1200],
                                 source=str(getattr(n, "source", "alpaca")), url=str(getattr(n, "url", ""))))
        return out


class AlpacaNewsDocuments:
    """V2: Benzinga articles via Alpaca as SourceDocuments with full content, multi-symbol lists,
    provider timestamps (created/updated), and a quality heuristic that discounts listicles."""
    name = "alpaca_benzinga"

    def __init__(self, api_key: str, secret_key: str, include_content: bool = True, max_content_chars: int = 6000):
        from alpaca.data.historical.news import NewsClient
        self._client = NewsClient(api_key, secret_key)
        self.include_content = include_content
        self.max_chars = max_content_chars

    def fetch_documents(self, symbols: List[str], since: datetime, limit: int = 50):
        from .documents import LOW_QUALITY_TITLE, SourceDocument, iso, now_utc
        import re
        from alpaca.data.requests import NewsRequest
        out = []
        seen = set()
        for sym in symbols:
            try:
                req = NewsRequest(symbols=sym.replace("-", "."), start=since, limit=limit, include_content=self.include_content)
                res = self._client.get_news(req)
                items = res.data.get("news", []) if hasattr(res, "data") else []
            except Exception as e:  # noqa: BLE001
                log.warning("alpaca news failed for %s: %s", sym, e)
                continue
            for n in items:
                pid = str(getattr(n, "id", ""))
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                syms = [str(s).replace(".", "-") for s in (getattr(n, "symbols", None) or [])]
                title = str(getattr(n, "headline", "") or "")
                raw = str(getattr(n, "content", "") or "") if self.include_content else ""
                content = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[: self.max_chars]
                quality = 0.70
                if LOW_QUALITY_TITLE.search(title):
                    quality = 0.30
                if len(syms) > 6:
                    quality = min(quality, 0.35)
                created = iso(getattr(n, "created_at", None))
                updated = iso(getattr(n, "updated_at", None))
                retrieved = now_utc().isoformat(timespec="seconds")
                out.append(SourceDocument(
                    doc_id=f"alpaca_news_{pid}", source=self.name, doc_type="news", symbols=syms, primary_symbol=sym,
                    title=title, summary=str(getattr(n, "summary", "") or "")[:1200], content=content,
                    url=str(getattr(n, "url", "") or ""), provider_id=pid, event_time=created, published_at=created or retrieved,
                    retrieved_at=retrieved, available_at=created or retrieved, source_quality=quality,
                    license_flags="alpaca_benzinga_subscriber_use",
                    meta={"author": str(getattr(n, "author", "") or ""), "updated_at": updated, "n_symbols": len(syms),
                          "source_name": str(getattr(n, "source", "") or "benzinga")}))
        return out
