"""SEC EDGAR: submissions (8-K/10-Q/10-K with acceptance timestamps) and XBRL fundamentals,
both point-in-time by construction. Free; requires a descriptive User-Agent. ~10 req/s max."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx

from .documents import SourceDocument, iso, now_utc

log = logging.getLogger(__name__)

ITEM_MAP = {  # 8-K item codes -> event type / materiality
    "1.01": ("material_agreement", 0.6), "1.02": ("agreement_termination", 0.6), "1.03": ("bankruptcy", 0.95),
    "1.05": ("cybersecurity_incident", 0.7), "2.01": ("acquisition_disposition", 0.75), "2.02": ("earnings", 0.85),
    "2.03": ("debt_obligation", 0.5), "2.04": ("debt_acceleration", 0.7), "2.05": ("restructuring", 0.6),
    "2.06": ("impairment", 0.6), "3.01": ("listing_notice", 0.7), "3.02": ("unregistered_sales", 0.4),
    "4.01": ("auditor_change", 0.6), "4.02": ("non_reliance_restatement", 0.9), "5.01": ("control_change", 0.8),
    "5.02": ("management", 0.6), "5.03": ("bylaws", 0.2), "5.07": ("shareholder_vote", 0.2), "7.01": ("reg_fd", 0.4),
    "8.01": ("other_event", 0.4), "9.01": ("exhibits", 0.1),
}
FORM_TYPE = {"10-Q": ("quarterly_report", 0.6), "10-K": ("annual_report", 0.6), "8-K": ("current_report", 0.5)}
CONCEPTS = {  # feature -> candidate XBRL tags (first with data wins)
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "op_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "equity": ["StockholdersEquity"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
}
_TAG = re.compile(r"<[^>]+>")


class Edgar:
    def __init__(self, user_agent: str, cache_dir: Path, rate_per_sec: float = 8.0):
        self.ua = user_agent
        self.cache = cache_dir
        self.cache.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"}, timeout=30.0)
        self._min_gap = 1.0 / rate_per_sec
        self._last = 0.0

    def _get(self, url: str) -> httpx.Response:
        gap = time.time() - self._last
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)
        self._last = time.time()
        r = self._client.get(url)
        r.raise_for_status()
        return r

    # ---------------------------------------------------------------- CIK map
    def cik_map(self, max_age_days: int = 7) -> Dict[str, Dict[str, Any]]:
        p = self.cache / "company_tickers.json"
        if not p.exists() or (time.time() - p.stat().st_mtime) > max_age_days * 86400:
            r = self._get("https://www.sec.gov/files/company_tickers.json")
            p.write_bytes(r.content)
        data = json.loads(p.read_text())
        out = {}
        for v in data.values():
            out[v["ticker"].upper()] = {"cik": f"{int(v['cik_str']):010d}", "name": v["title"]}
        return out

    # ---------------------------------------------------------------- filings
    def recent_filings(self, symbol: str, cik: str, forms: List[str], since: date, fetch_8k_text: bool = True,
                       max_chars: int = 6000) -> List[SourceDocument]:
        try:
            j = self._get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
        except httpx.HTTPError as e:
            log.warning("EDGAR submissions failed for %s: %s", symbol, e)
            return []
        rec = j.get("filings", {}).get("recent", {})
        docs: List[SourceDocument] = []
        n = len(rec.get("accessionNumber", []))
        for i in range(n):
            form = rec["form"][i]
            fdate = rec["filingDate"][i]
            if form not in forms or fdate < since.isoformat():
                continue
            acc = rec["accessionNumber"][i]
            accepted = rec.get("acceptanceDateTime", [None] * n)[i]
            items = [x.strip() for x in (rec.get("items", [""] * n)[i] or "").split(",") if x.strip()]
            primary = rec.get("primaryDocument", [""] * n)[i]
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{primary}"
            etype, importance = FORM_TYPE.get(form.replace("/A", ""), ("filing", 0.4))
            item_types = [ITEM_MAP.get(it, ("other_event", 0.3)) for it in items]
            if item_types:
                etype = max(item_types, key=lambda t: t[1])[0]
                importance = max(t[1] for t in item_types)
            title = f"{symbol} {form}" + (f" items {', '.join(items)}" if items else "") + f" filed {fdate}"
            content = ""
            if fetch_8k_text and form.startswith("8-K") and primary and any(it in ("1.01", "2.02", "2.01", "5.02", "7.01", "8.01", "4.02", "1.05") for it in items):
                content = self._doc_text(url, max_chars)
            pub = iso(accepted) or iso(datetime.fromisoformat(fdate).replace(hour=21, tzinfo=timezone.utc))
            retrieved = now_utc().isoformat(timespec="seconds")
            docs.append(SourceDocument(
                doc_id=f"sec_{acc.replace('-', '')}_{symbol}", source="sec", doc_type="filing", symbols=[symbol],
                primary_symbol=symbol, title=title, summary=content[:600] if content else f"{form} {', '.join(items)}".strip(),
                content=content, url=url, provider_id=acc, event_time=pub, published_at=pub, retrieved_at=retrieved,
                available_at=pub, effective_date=rec.get("reportDate", [None] * n)[i] or fdate, source_quality=0.95,
                license_flags="public_domain", meta={"form": form, "items": items, "event_type": etype, "importance": importance,
                                                     "report_date": rec.get("reportDate", [None] * n)[i]}))
        return docs

    def _doc_text(self, url: str, max_chars: int) -> str:
        try:
            html = self._get(url).text
        except httpx.HTTPError as e:
            log.warning("EDGAR document fetch failed %s: %s", url, e)
            return ""
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = _TAG.sub(" ", text)
        text = re.sub(r"&nbsp;|&#160;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"\s+", " ", text).strip()
        # skip the cover boilerplate; start at the first Item heading if present
        m = re.search(r"Item\s+\d\.\d\d", text)
        if m and m.start() < len(text) - 500:
            text = text[m.start():]
        return text[:max_chars]

    # ---------------------------------------------------------------- fundamentals
    def fundamentals(self, symbol: str, cik: str) -> List[Dict[str, Any]]:
        """Quarterly/annual facts with their FILED date (point-in-time). One request per concept."""
        rows: List[Dict[str, Any]] = []
        for feature, tags in CONCEPTS.items():
            for tag in tags:
                try:
                    j = self._get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json").json()
                except httpx.HTTPError:
                    continue
                units = j.get("units", {})
                unit = "USD/shares" if "USD/shares" in units else ("USD" if "USD" in units else next(iter(units), None))
                if not unit:
                    continue
                got = 0
                for e in units[unit]:
                    if e.get("form") not in ("10-Q", "10-K", "10-K/A", "10-Q/A") or not e.get("filed"):
                        continue
                    start, end = e.get("start"), e.get("end")
                    dur = None
                    if start and end:
                        dur = (date.fromisoformat(end) - date.fromisoformat(start)).days
                    if dur is not None and not (75 <= dur <= 100 or 350 <= dur <= 380):
                        continue    # keep clean quarters and full years only
                    rows.append({"symbol": symbol, "concept": feature, "period_end": end, "filed": e["filed"], "form": e.get("form"),
                                 "fp": (e.get("fp") or "") + ("_FY" if dur and dur > 300 else ""), "fy": e.get("fy"),
                                 "value": float(e["val"]), "unit": unit})
                    got += 1
                if got:
                    break
        return rows
