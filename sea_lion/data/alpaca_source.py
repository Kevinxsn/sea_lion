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
