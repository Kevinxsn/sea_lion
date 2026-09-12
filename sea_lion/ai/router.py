"""ModelRouter: the only place model calls happen.

Responsibilities (design §5): tier selection, prompt/schema versioning, cache by input hash,
budget counters, schema validation, bounded values, known tickers, evidence references,
retry-once-then-neutral. The router never sees broker credentials and returns only
validated pydantic objects.
"""
from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

from ..config import AICfg
from ..store import Store
from . import prompts
from .providers import ProviderError, build_provider
from .schemas import CheapOutput, EventFact, MainOutput, json_schema

log = logging.getLogger(__name__)


@dataclass
class TierStats:
    calls: int = 0
    cache_hits: int = 0
    invalid: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    skipped_budget: int = 0


@dataclass
class RouterState:
    cheap: TierStats = field(default_factory=TierStats)
    main: TierStats = field(default_factory=TierStats)
    budget_status: str = "ok"     # ok | soft_stop | hard_stop
    provider_unavailable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"cheap": self.cheap.__dict__, "main": self.main.__dict__, "budget_status": self.budget_status,
                "provider_unavailable": self.provider_unavailable}


class ModelRouter:
    def __init__(self, cfg: AICfg, store: Store, run_id: str, as_of: str, month_start: str,
                 known_tickers: List[str], replay: bool = False):
        self.cfg = cfg
        self.store = store
        self.run_id = run_id
        self.as_of = as_of
        self.month_start = month_start
        self.known = set(known_tickers)
        self.replay = replay          # replay: cache only, never call a provider
        self.state = RouterState()
        self._providers: Dict[str, Any] = {}
        self._refresh_budget()

    # ------------------------------------------------------------------ budget
    def _refresh_budget(self) -> None:
        b = self.cfg.budget
        day = self.store.cost_sum("ai.", self.as_of, self.as_of)
        month_cheap = self.store.cost_sum("ai.cheap", self.month_start, self.as_of)["usd"]
        month_main = self.store.cost_sum("ai.main", self.month_start, self.as_of)["usd"]
        month_total = month_cheap + month_main
        hard = (day["usd"] >= b.daily_usd or (day["tokens_in"] + day["tokens_out"]) >= b.daily_tokens
                or month_cheap >= b.monthly_cheap_usd or month_main >= b.monthly_main_usd
                or month_total >= b.monthly_total_usd)
        soft = month_total >= b.soft_stop_fraction * b.monthly_total_usd or day["usd"] >= b.soft_stop_fraction * b.daily_usd
        self.state.budget_status = "hard_stop" if hard else ("soft_stop" if soft else "ok")

    def _budget_allows(self, tier: str) -> bool:
        if self.state.budget_status == "hard_stop":
            return False
        if self.state.budget_status == "soft_stop" and tier == "main":
            return False   # main-model calls are the optional ones
        return True

    def _price(self, model: str, tin: int, tout: int) -> float:
        p = self.cfg.pricing.get(model) or self.cfg.pricing.get("default", {"input": 0.0, "output": 0.0})
        return tin / 1e6 * float(p.get("input", 0.0)) + tout / 1e6 * float(p.get("output", 0.0))

    def _provider(self, tier: str):
        if tier not in self._providers:
            tcfg = self.cfg.cheap if tier == "cheap" else self.cfg.main
            self._providers[tier] = build_provider(tcfg, self.cfg.allowed_hosts, self.cfg.allowed_providers)
        return self._providers[tier]

    # ------------------------------------------------------------------ core
    def _input_hash(self, tier: str, system: str, user: str, schema_name: str) -> str:
        tcfg = self.cfg.cheap if tier == "cheap" else self.cfg.main
        key = json.dumps({"tier": tier, "provider": tcfg.provider, "model": tcfg.model,
                          "prompt_version": self.cfg.prompt_version, "schema_version": self.cfg.schema_version,
                          "schema": schema_name, "system": system, "user": user}, sort_keys=True)
        return hashlib.sha256(key.encode()).hexdigest()

    def call_pass(self, tier: str, label: str, system: str, user: str, model_cls: Type[BaseModel],
                  validator=None) -> Optional[BaseModel]:
        """V2 generic pass: `tier` picks the provider config (cheap|main), `label` is recorded as the
        pass name in model_calls. Same cache, budget, validation and retry rules as V1 calls."""
        return self._call(tier, system, user, model_cls, validator, label=label)

    def _call(self, tier: str, system: str, user: str, model_cls: Type[BaseModel],
              validator, label: Optional[str] = None) -> Optional[BaseModel]:
        """Returns a validated object or None (=> neutral). Never raises for model problems."""
        stats = self.state.cheap if tier == "cheap" else self.state.main
        tcfg = self.cfg.cheap if tier == "cheap" else self.cfg.main
        log_tier = label or tier
        ih = self._input_hash(log_tier, system, user, model_cls.__name__)
        cached = self.store.cache_get(ih)
        if cached is not None:
            stats.cache_hits += 1
            self.store.log_model_call(run_id=self.run_id, tier=log_tier, provider=tcfg.provider, model=tcfg.model,
                                      prompt_version=self.cfg.prompt_version, schema_version=self.cfg.schema_version,
                                      input_hash=ih, cache_hit=1, input_tokens=0, output_tokens=0, latency_ms=0,
                                      cost_usd=0.0, valid=1, output_json=cached)
            try:
                return model_cls(**cached)
            except ValidationError:
                return None
        if self.replay:
            log.warning("replay: no cache entry for %s call; treating as neutral", tier)
            return None
        if not self._budget_allows(tier):
            stats.skipped_budget += 1
            return None
        attempts = 2 if self.cfg.retry_invalid_once else 1
        last_err = None
        max_tokens = tcfg.max_tokens
        for attempt in range(attempts):
            try:
                resp = self._provider(tier).complete_json(system, user, json_schema(model_cls), max_tokens,
                                                          tcfg.temperature)
            except ProviderError as e:
                stats.errors += 1
                last_err = str(e)
                self.state.provider_unavailable = True
                log.warning("%s provider error (attempt %d): %s", tier, attempt + 1, e)
                self._log(log_tier, tcfg, ih, None, 0, 0, 0, 0.0, valid=0, error=last_err, raw=None)
                continue
            cost = self._price(tcfg.model, resp.input_tokens, resp.output_tokens)
            stats.calls += 1
            stats.input_tokens += resp.input_tokens
            stats.output_tokens += resp.output_tokens
            stats.cost_usd += cost
            self.store.add_cost(self.as_of, f"ai.{tier}", cost, resp.input_tokens, resp.output_tokens,
                                self.run_id, tcfg.model)
            self._refresh_budget()
            obj, err = _parse_validate(resp.text, model_cls, validator)
            if resp.error:
                err = resp.error
                obj = None
            self._log(log_tier, tcfg, ih, obj, resp.input_tokens, resp.output_tokens, resp.latency_ms, cost,
                      valid=int(obj is not None), error=err, raw=resp.text)
            if obj is not None:
                self.store.cache_put(ih, log_tier, tcfg.provider, tcfg.model, self.cfg.prompt_version,
                                     self.cfg.schema_version, obj.model_dump())
                return obj
            stats.invalid += 1
            last_err = err
            if err and err.startswith("truncated_at_max_tokens"):
                max_tokens *= 2            # give the reasoning room once; then neutral
            log.warning("%s output invalid (attempt %d): %s", tier, attempt + 1, err)
        return None

    def _log(self, tier, tcfg, ih, obj, tin, tout, ms, cost, valid, error, raw):
        self.store.log_model_call(run_id=self.run_id, tier=tier, provider=tcfg.provider, model=tcfg.model,
                                  prompt_version=self.cfg.prompt_version, schema_version=self.cfg.schema_version,
                                  input_hash=ih, cache_hit=0, input_tokens=tin, output_tokens=tout, latency_ms=ms,
                                  cost_usd=cost, valid=valid, error=error,
                                  output_json=obj.model_dump() if obj is not None else None,
                                  raw_text=(raw or "")[:20000])

    # ------------------------------------------------------------------ tiers
    def extract_facts(self, events: List[Dict[str, Any]]) -> List[EventFact]:
        """Cheap tier: batch events -> validated facts. Unknown tickers/ids are dropped."""
        items = [{"event_id": e["event_id"], "ticker": e["symbol"], "published_at": e["published_at"],
                  "title": e.get("title", ""), "summary": (e.get("summary") or "")[:800]} for e in events]
        ids = {e["event_id"]: e["symbol"] for e in events}
        n = self.cfg.max_events_per_call
        batches = [items[i:i + n] for i in range(0, len(items), n)]

        def _one(batch: List[Dict[str, Any]]) -> List[EventFact]:
            batch_ids = {b["event_id"] for b in batch}

            def _v(out: CheapOutput) -> Optional[str]:
                for f in out.facts:
                    if f.event_id not in batch_ids:
                        return f"unknown event_id {f.event_id}"
                    if f.ticker != ids[f.event_id]:
                        return f"ticker mismatch for {f.event_id}"
                    if f.duplicate_of and f.duplicate_of not in batch_ids:
                        return f"unknown duplicate_of {f.duplicate_of}"
                return None

            out = self._call("cheap", prompts.CHEAP_SYSTEM, prompts.cheap_user(batch), CheapOutput, _v)
            return list(out.facts) if out is not None else []

        workers = max(1, int(getattr(self.cfg.cheap, "concurrency", 1)))
        facts: List[EventFact] = []
        if workers == 1 or len(batches) <= 1:
            for b in batches:
                facts.extend(_one(b))
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for res in ex.map(_one, batches):
                    facts.extend(res)
        return facts

    def assess(self, symbol: str, snapshot: Dict[str, Any], facts: List[EventFact]) -> Optional[MainOutput]:
        """Main tier: one candidate -> bounded thesis. None => neutral."""
        allowed_ids = {f.event_id for f in facts}
        fdicts = [f.model_dump() for f in facts]

        def _v(out: MainOutput) -> Optional[str]:
            if out.symbol != symbol:
                return f"symbol mismatch {out.symbol} != {symbol}"
            if out.symbol not in self.known:
                return "unknown symbol"
            bad = [e for e in out.evidence_ids if e not in allowed_ids]
            if bad:
                return f"evidence ids not in supplied set: {bad}"
            if not out.evidence_ids and abs(out.impact) > 0:
                return "non-zero impact without evidence"
            return None

        return self._call("main", prompts.MAIN_SYSTEM, prompts.main_user(symbol, snapshot, fdicts), MainOutput, _v)


def _parse_validate(text: str, model_cls: Type[BaseModel], validator):
    if not text or not text.strip():
        return None, "empty output"
    raw = text.strip()
    # tolerate ```json fences
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        # last resort: outermost braces
        s, t = raw.find("{"), raw.rfind("}")
        if s < 0 or t <= s:
            return None, f"invalid json: {e}"
        try:
            data = json.loads(raw[s:t + 1])
        except json.JSONDecodeError as e2:
            return None, f"invalid json: {e2}"
    try:
        obj = model_cls(**data) if isinstance(data, dict) else None
    except (ValidationError, TypeError) as e:
        return None, f"schema: {str(e)[:300]}"
    if obj is None:
        return None, "top-level JSON is not an object"
    err = validator(obj) if validator else None
    if err:
        return None, err
    return obj, None
