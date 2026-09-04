"""Typed configuration.

Policy lives in config/default.yaml (git-versioned); credentials live in .env.
`${ENV_VAR}` placeholders inside the YAML are substituted from the environment
so the YAML never contains secrets. The loaded config is hashed so every run
records exactly which policy produced it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"

Mode = Literal["backtest", "shadow", "sim", "paper", "live"]

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _sub_env(obj: Any) -> Any:
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, list):
        return [_sub_env(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sub_env(v) for k, v in obj.items()}
    return obj


class RunCfg(BaseModel):
    mode: Mode = "shadow"
    strategy_version: str = "v1.0"
    timezone: str = "America/New_York"
    decision_time_local: str = "16:30"


class UniverseCfg(BaseModel):
    benchmark: str = "SPY"
    symbols: Dict[str, str]  # symbol -> sector

    @property
    def tickers(self) -> List[str]:
        return sorted(self.symbols)

    def sector(self, sym: str) -> str:
        return self.symbols.get(sym, "Unknown")


class DataCfg(BaseModel):
    provider: Literal["yfinance", "alpaca"] = "yfinance"
    lookback_days: int = 420
    max_staleness_days: int = 4
    events_provider: Literal["yfinance", "alpaca", "file", "none"] = "yfinance"
    events_lookback_days: int = 3
    events_max_per_symbol: int = 5
    events_file: Optional[str] = None


class FeatureCfg(BaseModel):
    mom_short: int = 20
    mom_long: int = 60
    skip_last_day: int = 1
    trend_ma: int = 50
    vol_window: int = 20
    volume_ma: int = 20
    winsor_pct: float = 0.05
    min_dollar_volume: float = 2e7


class RegimeCfg(BaseModel):
    bull: float = 1.0
    neutral: float = 0.6
    bear: float = 0.3
    spy_vol_threshold: float = 0.25


class StrategyCfg(BaseModel):
    max_positions: int = 10
    ai_weight_cap: float = Field(0.20, ge=0.0, le=0.20)
    signal_weights: Dict[str, float]
    min_candidate_score: float = 0.0
    require_above_trend: bool = True
    regime: RegimeCfg = RegimeCfg()
    sizing: Literal["inverse_vol", "equal"] = "inverse_vol"


class RiskCfg(BaseModel):
    gross_exposure_max: float = 0.80
    single_position_max: float = 0.10
    max_positions: int = 10
    sector_max: float = 0.25
    daily_loss_lock: float = 0.02
    drawdown_safe_mode: float = 0.10
    turnover_max: float = 0.20
    min_order_notional: float = 10.0
    max_order_notional_frac: float = 0.10
    min_model_confidence: float = 0.60
    max_data_staleness_days: int = 4


class ModelTierCfg(BaseModel):
    provider: Literal["openai_compat", "anthropic"] = "openai_compat"
    base_url: str = "http://127.0.0.1:8324/v1"
    model: str = "deepseek-v4-flash-q4"
    max_tokens: int = 6000
    timeout_sec: float = 600
    temperature: float = 0.0
    concurrency: int = 1              # parallel requests (llama.cpp -np slots / vLLM batching)
    extra_body: Dict[str, Any] = {}   # passed through to the chat endpoint, e.g. {"chat_template_kwargs": {"enable_thinking": false}}


class BudgetCfg(BaseModel):
    daily_usd: float = 0.5
    monthly_cheap_usd: float = 5.0
    monthly_main_usd: float = 10.0
    monthly_total_usd: float = 25.0
    soft_stop_fraction: float = 0.8
    daily_tokens: int = 400_000


class AICfg(BaseModel):
    enabled: bool = True
    prompt_version: str = "v1"
    schema_version: str = "v1"
    main_top_n: int = 5
    importance_threshold: float = 0.5
    max_events_per_call: int = 12
    retry_invalid_once: bool = True
    quant_only_orders_allowed_modes: List[str] = ["sim", "paper"]
    cheap: ModelTierCfg = ModelTierCfg()
    main: ModelTierCfg = ModelTierCfg()
    allowed_providers: List[str] = ["openai_compat", "anthropic"]
    allowed_hosts: List[str] = ["127.0.0.1", "localhost", "api.anthropic.com"]
    budget: BudgetCfg = BudgetCfg()
    pricing: Dict[str, Dict[str, float]] = {"default": {"input": 0.0, "output": 0.0}}


class BrokerCfg(BaseModel):
    provider: Literal["sim", "alpaca"] = "sim"
    initial_cash: float = 2000.0
    limit_offset_bps: float = 15.0
    order_timeout_sec: int = 900
    sim_slippage_bps: float = 5.0
    sim_commission_usd: float = 0.0
    paper_sessions_required_before_live: int = 30
    allowed_order_types: List[str] = ["limit"]
    trading_hours_only: bool = True


class CostsCfg(BaseModel):
    data_monthly_usd: float = 0.0
    hosting_monthly_usd: float = 0.0
    monthly_ceiling_usd: float = 25.0
    pause_live_if_cost_frac_of_equity: float = 0.005


class ReportingCfg(BaseModel):
    write_html: bool = True
    write_json: bool = True


class Settings(BaseModel):
    run: RunCfg
    universe: UniverseCfg
    data: DataCfg = DataCfg()
    features: FeatureCfg = FeatureCfg()
    strategy: StrategyCfg
    risk: RiskCfg = RiskCfg()
    ai: AICfg = AICfg()
    broker: BrokerCfg = BrokerCfg()
    costs: CostsCfg = CostsCfg()
    reporting: ReportingCfg = ReportingCfg()

    # populated by load()
    config_path: Optional[str] = None
    config_hash: str = ""
    runtime_dir: str = str(REPO_ROOT / "runtime")

    @model_validator(mode="after")
    def _consistency(self) -> "Settings":
        if self.strategy.max_positions > self.risk.max_positions:
            raise ValueError("strategy.max_positions may not exceed risk.max_positions")
        if self.universe.benchmark not in self.universe.symbols:
            raise ValueError("benchmark must be part of the universe")
        return self

    # --- credentials (never stored on the model, read on demand) -------------
    def alpaca_keys(self) -> tuple[str, str]:
        if self.run.mode == "live":
            return os.environ.get("ALPACA_LIVE_API_KEY", ""), os.environ.get("ALPACA_LIVE_SECRET_KEY", "")
        return os.environ.get("ALPACA_PAPER_API_KEY", ""), os.environ.get("ALPACA_PAPER_SECRET_KEY", "")

    @property
    def is_live(self) -> bool:
        return self.run.mode == "live"

    @property
    def submits_orders(self) -> bool:
        return self.run.mode in ("sim", "paper", "live")

    @property
    def db_path(self) -> Path:
        # Paper and live must never share a database.
        name = {"live": "sea_lion_live.db", "paper": "sea_lion_paper.db"}.get(self.run.mode, "sea_lion_research.db")
        p = Path(self.runtime_dir) / "db"
        p.mkdir(parents=True, exist_ok=True)
        return p / name

    def dir(self, *parts: str) -> Path:
        p = Path(self.runtime_dir).joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p


def load(path: Optional[str | Path] = None, overrides: Optional[Dict[str, Any]] = None,
         mode: Optional[str] = None) -> Settings:
    """Load YAML + .env, substitute ${ENV}, apply overrides, validate, hash."""
    load_dotenv(REPO_ROOT / ".env", override=False)
    path = Path(path) if path else DEFAULT_CONFIG
    raw = yaml.safe_load(path.read_text())
    raw = _sub_env(raw)
    if overrides:
        raw = _deep_merge(raw, overrides)
    env_mode = mode or os.environ.get("SEA_LION_MODE")
    if env_mode:
        raw.setdefault("run", {})["mode"] = env_mode
    settings = Settings(**raw)
    settings.config_path = str(path)
    settings.config_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16]
    rt = os.environ.get("SEA_LION_RUNTIME_DIR")
    if rt:
        settings.runtime_dir = rt
    return settings


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
