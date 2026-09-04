# AI-Assisted Quantitative Automated Trading System

**V1 MVP design for a $2,000 experimental U.S. equities account**

| Field | Value |
|---|---|
| Status | Draft for implementation |
| Version | 1.0 |
| Date | August 25, 2026 |
| Primary objective | Launch safely in paper trading, then validate with minimal live capital |

> **V1 decision:** Use AI to structure unstructured information and rank candidates. Use deterministic code for position sizing, risk limits, order validation, and execution. AI never receives unrestricted broker authority.

## 1. Executive Summary

This MVP is a low-frequency, long-only research system for liquid U.S. equities and ETFs. It runs once per trading day, combines simple quantitative signals with AI-assisted event analysis, and proposes a small target portfolio. A deterministic risk engine may reduce or reject every proposal before orders reach the broker.

The initial success criterion is operational reliability and decision traceability, not profit. The system must complete an end-to-end paper-trading cycle, reproduce every decision from stored inputs, stay within cost and risk budgets, and fail safely when data, models, or broker services are unavailable.

## 2. Goals, Non-Goals, and Assumptions

### Goals

- Run a complete daily research-to-order workflow with minimal infrastructure.
- Test whether combined quantitative and AI-derived signals add value after costs.
- Protect a small account with hard, machine-enforced limits and full audit logs.
- Keep model and data spending proportionate to a $2,000 experiment.

### Non-goals

- High-frequency or intraday market-making.
- Options, short selling, leverage, or crypto.
- Autonomous strategy invention or self-modifying production code.
- Guaranteed returns, tax optimization, or investment advice for other people.
- Training a proprietary foundation model or buying institutional data feeds.

### Operating assumptions

| Dimension | V1 assumption |
|---|---|
| Account | $2,000 experimental individual account; loss is tolerable but bounded by controls. |
| Universe | 20–50 highly liquid U.S. large-cap stocks and broad ETFs; fractional shares allowed. |
| Cadence | One decision cycle after market close; orders submitted for the next session. |
| Holding period | Approximately 2–20 trading days; no same-day turnover target. |
| Strategy | Long-only cash account with no margin. |
| Benchmarks | SPY, cash, and a quant-only version of the strategy. |

## 3. High-Level Architecture

V1 is a scheduled batch pipeline with a single repository, one database, one worker process, and one broker adapter. Every stage writes an immutable run record before the next stage begins.

```text
Market data + approved events
            |
            v
    Data validation
            |
            v
  Quant feature engine
            |
            +------> Cheap AI model: extraction and filtering
            |                         |
            |                         v
            |                Main AI model: top candidates
            |                         |
            +-------------------------+
                            |
                            v
                    Signal ensemble
                            |
                            v
                  Portfolio proposal
                            |
                            v
              Deterministic risk engine
                            |
                            v
                   Broker execution
                            |
                            v
             Reconciliation + reporting
```

### Core components

| Component | Responsibility | V1 implementation |
|---|---|---|
| Scheduler | Starts daily runs and retries safe stages. | Cron or managed scheduler |
| Data adapter | Normalizes market and event inputs. | One market-data source plus optional news source |
| Feature engine | Calculates deterministic quantitative features. | Python data pipeline |
| Model router | Controls model tier, prompt, schema, cache, and budget. | Provider-neutral API wrapper |
| Strategy engine | Combines signals into candidate scores. | Versioned configuration |
| Risk engine | Constrains targets and orders. | Pure deterministic functions |
| Broker adapter | Places, cancels, and reconciles orders. | Alpaca paper/live endpoints |
| Store and reporting | Persists inputs, decisions, orders, and metrics. | SQLite initially; daily HTML/JSON summary |

## 4. Daily Data Flow

1. **Ingest:** Fetch adjusted daily bars, benchmark data, and recent approved company events. Record source time, market date, and retrieval status.
2. **Validate:** Reject stale, missing, duplicated, or structurally invalid observations. Use adjusted prices consistently.
3. **Compute features:** Calculate momentum, trend, realized volatility, volume, liquidity, and market-regime features without future leakage.
4. **Route AI work:** Use the cheap model to extract event facts. Send only important candidates to the main model for a bounded thesis and risk assessment.
5. **Score and allocate:** Blend normalized quantitative signals with a capped AI score, then convert scores to target weights.
6. **Enforce risk:** Apply exposure, position, loss, turnover, liquidity, and stale-data limits. Reject invalid or ambiguous actions.
7. **Execute and reconcile:** Create idempotent orders, submit them through the broker adapter, confirm fills, and compare broker state with local state.
8. **Evaluate:** Record performance, costs, forecast calibration, failures, and the complete decision trace.

## 5. Model Routing and Decision Contract

V1 uses two cost tiers behind the same interface. Model choice is configuration rather than strategy logic. Models must return validated JSON. Prose may be stored for inspection but is never parsed to create orders.

| Tier | Purpose | Invocation policy | Required output |
|---|---|---|---|
| Cheap model | Ticker/event extraction, relevance, sentiment, and deduplication | Batch eligible items; no deep reasoning | Ticker, event type/time, sentiment `[-1,1]`, importance `[0,1]`, confidence |
| Main model | Assess material candidates and identify thesis risks | Only top 3–5 candidates or events above an importance threshold | Bull case, bear case, horizon, impact `[-1,1]`, confidence, risk flags, evidence IDs |

### Routing rules

- Skip AI calls when inputs are stale, the daily budget is exhausted, or no new material event exists.
- Cache by normalized input hash, prompt version, model version, and schema version.
- Require schema validation, bounded values, known tickers, and evidence references.
- Retry invalid output once, then treat the AI score as neutral.
- Cap the AI contribution at 20% of the final candidate score in V1.
- Keep the quant-only score separately observable.
- Never let model text set order type, account identifier, quantity, or broker credentials.

### Example decision object

```json
{
  "symbol": "MSFT",
  "as_of": "YYYY-MM-DD",
  "horizon_days": 5,
  "quant_score": 0.42,
  "ai_score": 0.18,
  "confidence": 0.71,
  "risk_flags": [],
  "evidence_ids": ["evt_123"],
  "prompt_version": "v1"
}
```

If AI is unavailable, the system may generate a quant-only proposal for research logging. Live order submission remains disabled unless the run mode explicitly permits that fallback.

## 6. Quantitative Signals

V1 uses a small, explainable signal set. Features are winsorized, standardized within the universe, and computed only from information available at the decision timestamp.

| Signal | Definition | Initial role |
|---|---|---|
| Momentum | 20-day and 60-day total return, excluding the most recent day | Primary ranking signal |
| Trend | Price relative to the 50-day moving average | Direction filter |
| Volatility | 20-day annualized realized volatility | Inverse-volatility sizing |
| Volume/liquidity | Dollar volume and unusual-volume ratio | Eligibility and event confirmation |
| Market regime | SPY trend and volatility state | Scale total exposure |
| AI event score | Bounded impact from recent, cited events | Maximum 20% of ensemble score |

## 7. Portfolio Construction and Risk Controls

The strategy ranks eligible symbols, selects at most ten, and assigns inverse-volatility weights subject to caps. Cash is a valid allocation. The risk engine is the final authority and returns explicit rejection reasons.

| Control | Initial limit | Behavior |
|---|---:|---|
| Gross exposure | 80% of equity | Keep at least 20% cash. |
| Single position | 10% of equity | Clamp target weight; no exceptions. |
| Positions | Maximum 10 | Ignore lower-ranked candidates. |
| Sector concentration | 25% of equity | Reduce or reject the newest target. |
| Daily loss lock | 2% of start-of-day equity | Cancel open buys and block new exposure until the next session. |
| Portfolio drawdown | 10% from high-water mark | Enter safe mode; liquidation requires manual approval. |
| Turnover | 20% of equity per day | Scale non-risk-reducing trades. |
| Order size | $10 minimum; 10% maximum | Avoid dust orders and oversizing. |
| Data freshness | Latest completed market session | Block order generation when stale or inconsistent. |
| Model confidence | Below 0.60 is neutral | Set AI score to zero; never bypass risk rules. |

> Stop orders are not the primary protection for this low-frequency MVP because gap risk can bypass them. Exposure caps, cash reserve, diversification, daily loss locks, and manual kill controls are the primary safeguards.

## 8. Broker Integration and Execution

Use Alpaca paper trading for V1 because it supports API-based order management and fractional U.S. equities. Keep the broker behind an adapter so another broker can be added later. Paper and live environments must use separate credentials and explicit configuration.

- Generate target-position deltas from a fresh broker position snapshot; do not assume local state is authoritative.
- Use notional marketable limit orders during regular market hours. Cancel unfilled orders after a fixed timeout.
- Derive a deterministic `client_order_id` from the run ID, symbol, side, and strategy version to prevent duplicates.
- Immediately before submission, repeat price, buying-power, position, market-hours, and risk checks using fresh values.
- Reconcile fills, fees, cash, and positions after submission. Any mismatch enters safe mode.

## 9. Failure Handling

| Failure | Default response |
|---|---|
| Market or event data is missing/stale | Abort before model calls and orders; alert and log source diagnostics. |
| Model timeout, rate limit, or invalid JSON | Retry once with backoff; otherwise use a neutral AI score and block live trading unless fallback is enabled. |
| Broker unavailable or response is ambiguous | Do not blindly retry submission. Query by `client_order_id`, reconcile, then alert. |
| Partial fill or rejected order | Recompute exposure from confirmed fills, cancel unsafe remainder, and do not chase price. |
| Local and broker state disagree | Enter safe mode, block new exposure, and preserve risk-reducing actions for manual review. |
| Process crashes | Resume from persisted checkpoints; idempotency prevents duplicate orders. |

## 10. Cost Budget

The system enforces hard spend caps and usage counters. Prices vary by vendor, so V1 budgets in dollars and tokens rather than depending on a specific published rate.

| Category | Monthly target | Hard ceiling | Control |
|---|---:|---:|---|
| Cheap-model API | $1–3 | $5 | Batching, caching, short structured outputs |
| Main-model API | $2–6 | $10 | Top 3–5 candidates per day; bounded context |
| Market/news data | $0 | $10 | Free broker data and limited approved sources |
| Hosting/storage | $0–5 | $10 | Local machine or minimal scheduled compute |
| **Total** | **$3–14** | **$25** | Disable optional calls at 80%; stop at 100% |

Pause live deployment if recurring system cost exceeds 0.5% of account equity per month—about $10 on a $2,000 account—unless the excess is explicitly treated as a research expense.

## 11. Backtest, Paper, and Live Rollout

| Phase | Minimum duration | Entry/exit gate |
|---|---|---|
| Backtest | Five or more years where available | Walk-forward evaluation with no look-ahead. Compare with SPY, cash, and the quant-only baseline. Exit when results reproduce and sensitivity is acceptable. |
| Shadow | 10 trading days | Generate decisions without orders. Exit after zero stale-data or duplicate-order defects. |
| Paper | 30 trading days | Execute through the paper broker. Exit after reconciliation is reliable and risk limits trigger correctly in tests. |
| Live pilot | $200–500 for 30 days | Require manual enablement each day; no automatic scaling. Exit only after operational review. |
| Live V1 | Up to $2,000 | Scale gradually while retaining the daily kill switch and weekly review. |

Promotion depends on operational gates, not a profitable streak. Backtests must include spread/slippage assumptions, survivorship-bias controls, and exact historical information timestamps. If historical event data cannot be reconstructed reliably, evaluate the AI layer prospectively during shadow and paper phases instead of fabricating a backtest.

## 12. Logging and Metrics

### Required logs

- **Run trace:** Run ID, timestamps, code/config versions, input hashes, freshness, stage status, and errors.
- **Model trace:** Provider/model, prompt and schema versions, token use, latency, cache hit, structured output, validation status, and estimated cost.
- **Trading trace:** Proposals, risk decisions, order IDs, acknowledgements, fills, slippage, fees, cash, positions, and reconciliation results.

### Required metrics

- Net return, volatility, maximum drawdown, Sharpe ratio, hit rate, turnover, and exposure.
- Benchmark excess return and quant-only versus AI-assisted performance delta.
- Directional accuracy, confidence calibration, and performance by score bucket and market regime.
- API, data, infrastructure, and trading costs.
- Pipeline success rate, broker reconciliation errors, model validation failures, and duplicate-order prevention events.

Report uncertainty clearly when the sample is small.

## 13. Basic Security and Operational Controls

- Store broker and model credentials in environment-backed secrets, never in source code, prompts, logs, or notebooks.
- Use least-privilege API keys. Disable withdrawal and account-management permissions where supported.
- Separate paper and live credentials, databases, and configuration.
- Require both an explicit live-mode flag and a manual confirmation token.
- Allowlist symbols, model providers, data hosts, order types, and trading hours.
- Treat news and filings as untrusted data that cannot change system instructions.
- Redact credentials and personal account data from logs.
- Encrypt the workstation and restrict local file permissions.
- Provide a one-action kill switch that cancels open orders and disables new exposure.
- Do not automatically liquidate the portfolio solely because of a software failure.

## 14. MVP Implementation Plan

| Week | Deliverable | Acceptance check |
|---:|---|---|
| 1 | Repository, configuration, SQLite schema, market-data adapter, and feature calculations | One historical day can be replayed deterministically. |
| 2 | Model router, JSON schemas, caching, candidate filter, and cost counters | Invalid and adversarial inputs fail closed. |
| 3 | Strategy, portfolio construction, risk engine, and paper-broker adapter | Unit tests cover every limit and duplicate-order scenario. |
| 4 | End-to-end scheduler, reconciliation, daily report, alerts, and shadow run | Ten consecutive shadow runs complete with reproducible outputs. |
| 5–8 | Paper-trading observation and reliability fixes | Thirty trading days meet the paper promotion gate. |

## 15. V1 Acceptance Criteria

- [ ] A run can be replayed from stored inputs and produces the same pre-trade proposal.
- [ ] No order can bypass risk validation; every rejection has a machine-readable reason.
- [ ] Duplicate-submission tests create at most one broker order per intended action.
- [ ] Stale data, invalid model output, broker ambiguity, and state mismatch fail safely.
- [ ] Daily reports separate strategy return, benchmark return, trading costs, and API/data costs.
- [ ] Paper trading completes for 30 sessions before any live order is permitted.

---

> **Not investment advice:** This document describes a software research experiment. Automated trading can lose the full amount deployed, and paper results may not reproduce in live markets. Review current broker rules, taxes, and applicable law before live use.
