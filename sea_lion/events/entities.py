"""Entity registry: ticker <-> CIK <-> company name <-> aliases, plus resolution of mentions in text."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Hand-curated aliases for the V1 universe; SEC names are added automatically. Config may extend.
DEFAULT_ALIASES: Dict[str, List[str]] = {
    "AAPL": ["apple"], "MSFT": ["microsoft"], "NVDA": ["nvidia"], "AVGO": ["broadcom"], "ORCL": ["oracle"],
    "AMZN": ["amazon", "aws", "amazon web services"], "TSLA": ["tesla"], "HD": ["home depot"],
    "GOOGL": ["alphabet", "google", "youtube", "waymo"], "META": ["meta platforms", "facebook", "instagram"],
    "NFLX": ["netflix"], "BRK-B": ["berkshire hathaway", "berkshire"], "JPM": ["jpmorgan", "jp morgan", "jpmorgan chase"],
    "V": ["visa inc", "visa"], "MA": ["mastercard"], "BAC": ["bank of america"], "UNH": ["unitedhealth", "united health"],
    "LLY": ["eli lilly", "lilly"], "JNJ": ["johnson & johnson", "johnson and johnson", "j&j"], "ABBV": ["abbvie"],
    "MRK": ["merck"], "XOM": ["exxon", "exxonmobil", "exxon mobil"], "CVX": ["chevron"], "COST": ["costco"],
    "WMT": ["walmart"], "PG": ["procter & gamble", "procter and gamble", "p&g"], "KO": ["coca-cola", "coca cola", "coke"],
    "PEP": ["pepsico", "pepsi"], "CAT": ["caterpillar"], "GE": ["ge aerospace", "general electric"], "HON": ["honeywell"],
    "LIN": ["linde"], "NEE": ["nextera", "nextera energy"], "PLD": ["prologis"],
    "SPY": ["s&p 500 etf", "spdr s&p 500"], "QQQ": ["nasdaq-100 etf", "invesco qqq"], "IWM": ["russell 2000 etf"],
    "XLK": ["technology select sector"], "XLF": ["financial select sector"], "XLV": ["health care select sector"],
    "XLE": ["energy select sector"], "GLD": ["spdr gold"], "TLT": ["20+ year treasury etf"],
}
SECTOR_ETF = {"Technology": "XLK", "Financials": "XLF", "Health Care": "XLV", "Energy": "XLE"}
_CLEAN = re.compile(r"[^a-z0-9&\- ]+")
_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|company|ltd|plc|holdings?|group|the|class [abc]|common stock|delaware|new)\b\.?")


def normalize_name(name: str) -> str:
    n = _CLEAN.sub(" ", name.lower())
    n = _SUFFIX.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


class EntityRegistry:
    def __init__(self, universe: Dict[str, str], cik_map: Optional[Dict[str, Dict[str, Any]]] = None,
                 aliases: Optional[Dict[str, List[str]]] = None):
        self.universe = dict(universe)            # symbol -> sector
        self.cik_map = cik_map or {}
        self.aliases: Dict[str, List[str]] = {}
        for sym in universe:
            al = set(a.lower() for a in DEFAULT_ALIASES.get(sym, []))
            if aliases and sym in aliases:
                al |= {a.lower() for a in aliases[sym]}
            info = self.cik_map.get(sym) or self.cik_map.get(sym.replace("-", "."))
            if info and info.get("name"):
                nn = normalize_name(info["name"])
                if len(nn) > 3:
                    al.add(nn)
            self.aliases[sym] = sorted(al, key=len, reverse=True)
        self._ticker_re = re.compile(r"(?<![A-Za-z])\$?(" + "|".join(re.escape(s) for s in sorted(universe, key=len, reverse=True)) + r")(?![A-Za-z])")

    def cik(self, sym: str) -> Optional[str]:
        info = self.cik_map.get(sym) or self.cik_map.get(sym.replace("-", "."))
        return info["cik"] if info else None

    def name(self, sym: str) -> str:
        info = self.cik_map.get(sym) or self.cik_map.get(sym.replace("-", "."))
        return info["name"] if info else sym

    def sector(self, sym: str) -> str:
        return self.universe.get(sym, "Unknown")

    def sector_etf(self, sym: str) -> str:
        return SECTOR_ETF.get(self.sector(sym), "SPY")

    def resolve(self, text: str, hint_symbols: Optional[List[str]] = None) -> List[Tuple[str, str, float, str]]:
        """Return (symbol, span, confidence, method) mentions found in text."""
        out: List[Tuple[str, str, float, str]] = []
        seen = set()
        for m in self._ticker_re.finditer(text or ""):
            sym = m.group(1)
            if sym in self.universe and (sym, "ticker") not in seen and (len(sym) > 2 or m.group(0).startswith("$")):
                out.append((sym, m.group(0), 0.9, "ticker"))
                seen.add((sym, "ticker"))
        low = (text or "").lower()
        for sym, als in self.aliases.items():
            for a in als:
                if a and a in low:
                    if (sym, "alias") not in seen:
                        out.append((sym, a, 0.8 if len(a) > 4 else 0.6, "alias"))
                        seen.add((sym, "alias"))
                    break
        for sym in hint_symbols or []:
            if sym in self.universe and not any(o[0] == sym for o in out):
                out.append((sym, "", 0.5, "provider_tag"))
        return out
