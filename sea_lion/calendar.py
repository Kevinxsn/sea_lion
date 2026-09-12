"""Trading calendar (design §5.1, §14): official sessions with early closes from Alpaca, cached
in the store; a weekday+known-holiday fallback when the broker calendar is unavailable.
The calendar may permit or skip a run; it cannot trade."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .store import Store

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# NYSE full-day holidays as a fallback only (observed dates). Extend as years pass.
_FALLBACK_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}
_FALLBACK_EARLY = {"2026-11-27": "13:00", "2026-12-24": "13:00", "2027-11-26": "13:00"}


@dataclass
class Session:
    date: str
    open: str    # "09:30"
    close: str   # "16:00" or "13:00"

    @property
    def early_close(self) -> bool:
        return self.close != "16:00"


class TradingCalendar:
    def __init__(self, store: Store, provider: str = "alpaca", api_key: str = "", secret_key: str = "",
                 paper: bool = True, cache_days: int = 120):
        self.store = store
        self.provider = provider
        self._keys = (api_key, secret_key, paper)
        self.cache_days = cache_days
        self._sessions: Dict[str, Session] = {}
        self.source = "none"
        self._load()

    # ---------------------------------------------------------------- loading
    def _load(self) -> None:
        cached = self.store.kv_get("calendar:sessions") or {}
        meta = self.store.kv_get("calendar:meta") or {}
        today = date.today().isoformat()
        fresh = bool(cached) and meta.get("fetched_on") == today and meta.get("end", "") >= (date.today() + timedelta(days=30)).isoformat()
        if not fresh and self.provider == "alpaca" and self._keys[0] and self._keys[1]:
            try:
                cached = self._fetch_alpaca()
                self.store.kv_set("calendar:sessions", cached)
                self.store.kv_set("calendar:meta", {"fetched_on": today, "end": max(cached), "source": "alpaca"})
                meta = {"source": "alpaca"}
            except Exception as e:  # noqa: BLE001
                log.warning("alpaca calendar fetch failed (%s); using %s", e, "cached" if cached else "fallback")
        if cached:
            self._sessions = {d: Session(d, v["open"], v["close"]) for d, v in cached.items()}
            self.source = meta.get("source", "cache")
        else:
            self._sessions = self._fallback()
            self.source = "fallback"

    def _fetch_alpaca(self) -> Dict[str, Dict[str, str]]:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetCalendarRequest
        k, s, paper = self._keys
        c = TradingClient(k, s, paper=paper)
        start = date.today() - timedelta(days=45)
        end = date.today() + timedelta(days=self.cache_days)
        out = {}
        for d in c.get_calendar(GetCalendarRequest(start=start, end=end)):
            out[str(d.date)] = {"open": str(d.open)[-8:-3] if len(str(d.open)) > 5 else str(d.open),
                                "close": str(d.close)[-8:-3] if len(str(d.close)) > 5 else str(d.close)}
        if not out:
            raise RuntimeError("empty calendar")
        return out

    def _fallback(self) -> Dict[str, Session]:
        out = {}
        d = date.today() - timedelta(days=45)
        while d <= date.today() + timedelta(days=self.cache_days):
            iso = d.isoformat()
            if d.weekday() < 5 and iso not in _FALLBACK_HOLIDAYS:
                out[iso] = Session(iso, "09:30", _FALLBACK_EARLY.get(iso, "16:00"))
            d += timedelta(days=1)
        return out

    # ---------------------------------------------------------------- queries
    def is_session(self, d: date | str) -> bool:
        return (d if isinstance(d, str) else d.isoformat()) in self._sessions

    def session(self, d: date | str) -> Optional[Session]:
        return self._sessions.get(d if isinstance(d, str) else d.isoformat())

    def previous_session(self, d: date | str, inclusive: bool = False) -> Optional[Session]:
        iso = d if isinstance(d, str) else d.isoformat()
        keys = sorted(k for k in self._sessions if (k <= iso if inclusive else k < iso))
        return self._sessions[keys[-1]] if keys else None

    def next_session(self, d: date | str, inclusive: bool = False) -> Optional[Session]:
        iso = d if isinstance(d, str) else d.isoformat()
        keys = sorted(k for k in self._sessions if (k >= iso if inclusive else k > iso))
        return self._sessions[keys[0]] if keys else None

    def sessions_between(self, start: str, end: str) -> List[str]:
        return sorted(k for k in self._sessions if start <= k <= end)

    def nth_session_after(self, d: str, n: int) -> Optional[str]:
        keys = sorted(k for k in self._sessions if k > d)
        return keys[n - 1] if len(keys) >= n else None

    def last_completed_session(self, now: Optional[datetime] = None, settle_minutes: int = 15) -> str:
        """The most recent session whose close (plus settle) is in the past, in ET."""
        now = now.astimezone(ET) if now else datetime.now(ET)
        today = now.date().isoformat()
        s = self.session(today)
        if s:
            h, m = map(int, s.close.split(":"))
            if now.time() >= (datetime.combine(now.date(), time(h, m)) + timedelta(minutes=settle_minutes)).time():
                return today
        p = self.previous_session(today)
        return p.date if p else today

    def market_state(self, now: Optional[datetime] = None) -> Dict[str, object]:
        now = now.astimezone(ET) if now else datetime.now(ET)
        s = self.session(now.date().isoformat())
        if not s:
            nxt = self.next_session(now.date().isoformat())
            return {"is_session": False, "is_open": False, "next_session": nxt.date if nxt else None, "source": self.source}
        h1, m1 = map(int, s.open.split(":")); h2, m2 = map(int, s.close.split(":"))
        is_open = time(h1, m1) <= now.time() < time(h2, m2)
        return {"is_session": True, "is_open": is_open, "open": s.open, "close": s.close, "early_close": s.early_close,
                "now_et": now.isoformat(timespec="minutes"), "source": self.source}

    def in_decision_window(self, window: List[str], now: Optional[datetime] = None) -> bool:
        st = self.market_state(now)
        if not st["is_session"]:
            return False
        now = now.astimezone(ET) if now else datetime.now(ET)
        lo = time(*map(int, window[0].split(":")))
        hi = time(*map(int, window[1].split(":")))
        close = time(*map(int, str(st["close"]).split(":")))
        hi = min(hi, (datetime.combine(now.date(), close) - timedelta(minutes=20)).time())
        return lo <= now.time() <= hi
