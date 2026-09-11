# Sea Lion V2 — Evidence-Driven AI-Assisted Quantitative Trading System

**Design for a small-capital, low-frequency U.S. equities research system**

| Field | Value |
|---|---|
| Status | Revised draft incorporating the first week of paper-trading evidence |
| Version | 2.1 |
| Date | September 11, 2026 |
| Starting point | Working Sea Lion V1 plus the September 4–11 paper-trading review |
| Intended capital | $2,000 experimental account |
| Current deployment | Alpaca paper trading with local DeepSeek V4 Flash |
| Owner / alert contact | Kevin — `kevinxsn@outlook.com` |

> **V2 decision:** Spend the additional local-model capacity on evidence quality, verification, explicit uncertainty, and evaluation—not on giving an AI agent direct control of the broker. Quantitative calculations, portfolio constraints, risk decisions, and order execution remain deterministic.

## 1. Executive Summary

Sea Lion V2 upgrades the working V1 pipeline into a more comprehensive, evidence-driven decision system. It remains a once-daily, long-only strategy for liquid U.S. stocks and ETFs, but it adds point-in-time data handling, multiple information sources, global event deduplication, multi-pass AI analysis, calibrated forecasts, portfolio-aware risk, and controlled experiment design.

The local DeepSeek V4 Flash deployment changes the primary constraint. Token cost is no longer the main bottleneck; data quality, correlated model errors, latency, historical validity, and overfitting are. V2 therefore allows deeper analysis of each material event while retaining bounded stages, structured outputs, evidence citations, and deterministic failure behavior.

V2 does not assume that more reasoning automatically creates alpha. It is designed to answer that question experimentally by running three comparable decision arms:

1. Quant-only baseline.
2. V1-style headline AI overlay.
3. V2 verified-event AI overlay.

The immediate objective is not to maximize returns. It is to produce more evidence-aware, better calibrated, auditable decisions and determine whether the V2 AI overlay improves prospective results after turnover, spread, and operational costs.

The first week of Alpaca paper trading establishes a concrete baseline. The system completed three decision cycles without crashes, duplicate orders, ambiguous broker responses, or invalid model outputs. However, an incomplete broker-status list caused a false safe-mode entry, skipped-session runs delayed reconciliation, and the absence of external alerts left the system blocked for two days. The AI overlay did not change a single portfolio decision. V2.1 therefore puts reconciliation correctness, alerting, book-level risk, low-turnover portfolio construction, and measurable AI attribution ahead of additional strategy complexity.

## 2. V1 Baseline and First-Week Lessons

The existing system already provides a strong operational base:

- Daily modes for backtest, shadow, sim, paper, and live.
- A universe of approximately 43 liquid U.S. stocks and ETFs.
- Deterministic momentum, trend, volatility, volume, and market-regime signals.
- A local DeepSeek V4 Flash model for both extraction and candidate analysis.
- An 80% quant / 20% AI ensemble.
- Hard exposure, position, sector, turnover, daily-loss, drawdown, and order-size limits.
- Alpaca broker integration with idempotent order IDs and reconciliation.
- Structured run, model, risk, and execution records.

The initial implementation and first week exposed the most valuable V2 opportunities:

| V1 behavior | V2 response |
|---|---|
| Yahoo headlines and summaries are the main event source. | Add Alpaca/Benzinga news, SEC filings, macro data, and source-quality metadata. |
| Duplicate detection is limited and mostly local to a batch. | Cluster the same real-world event globally across symbols, sources, and runs. |
| The main model receives compact event names, not a verified evidence packet. | Retrieve permitted content and build a cited, contradiction-aware event dossier. |
| One model produces a thesis and self-reported confidence. | Separate extraction, verification, analysis, skeptical review, and synthesis passes. |
| Confidence below 0.60 is neutral, but confidence is not empirically calibrated. | Record every forecast outcome and calibrate probability estimates prospectively. |
| Quant features are primarily price and volume based. | Add point-in-time fundamentals, event, cross-sectional, and portfolio-risk features. |
| Backtest is quant-only and uses a fixed present-day universe. | Improve point-in-time evaluation and use prospective experiments for the AI layer. |
| Alerting and operational health checks are limited. | Add source, model, broker, data-quality, latency, and exposure alerts. |

V2 should be implemented as a sequence of migrations behind existing interfaces. It should not discard the stable V1 broker, risk, run-mode, or reconciliation behavior.

### 2.1 Observed paper-trading baseline: September 4–11, 2026

| Observation | Evidence | Design consequence |
|---|---:|---|
| Cron firings | 5 | Scheduling worked across a holiday, but every invocation must produce an observable result. |
| Completed decision cycles | 3 | Performance conclusions remain premature. Operational defects still take priority. |
| Alpaca orders | 21 total; 19 filled; 2 expired | Morning marketable-limit execution is retained; after-close live/paper submission is prohibited. |
| Duplicate or ambiguous submissions | 0 | Preserve deterministic client order IDs and query-before-retry behavior. |
| Invalid model outputs | 0 of 75 cumulative calls reported | Preserve strict schemas and validation; model-output reliability is not the current bottleneck. |
| False safe-mode entries | 1 | Broker lifecycle states must have one authoritative definition and full regression coverage. |
| Time until false safe mode was noticed | 2 trading days | Alerts are a release blocker, not a reporting enhancement. |
| Actual versus intended exposure | About 30% versus approximately 76% before the regime change | Report exposure attribution; low exposure—not demonstrated alpha—explains the early relative result. |
| Paper account return | -0.29% over a very small sample | Treat as noise; do not tune the strategy to this week. |
| AI-versus-quant portfolio difference | Zero in every completed cycle | Measure rank, membership, weight, and return differences before changing the AI weight. |
| Main-tier confidence | 13 of 24 assessments exactly 0.60 | Remove the revealed threshold and replace self-reported confidence with calibrated sign probabilities. |
| Event mix | 43 of 144 facts macro; 25 analyst; only 5 earnings/guidance | Prioritize primary company events and keep generic macro material out of company analysis unless highly material. |
| Runtime | 9.3–10.8 minutes; AI approximately 95% | Split slow research from the morning execution-critical path and add stage deadlines. |

The account's apparent relative performance versus SPY during this week is not evidence of skill because the portfolio was only about 30% invested. V2 reports return together with realized and target exposure so this distinction is always visible.

### 2.2 V2.1 priority order

1. Reconciliation correctness and safe-mode integrity.
2. Immediate operational alerts and a visible one-line run result.
3. Actual-book risk, complete exits, and broker-account validation.
4. Rank hysteresis and explicit AI-versus-quant attribution.
5. Faster, staged local-model processing.
6. Higher-quality news, filings, calibration, and expanded quantitative features.

Items 1–3 must be complete and verified in paper before the richer V2 research layer is allowed to influence orders.

## 3. Goals, Non-Goals, and Assumptions

### 3.1 Goals

- Improve decision quality by combining independent categories of evidence: price behavior, filings, company news, market context, and macro conditions.
- Make every AI conclusion traceable to stored evidence available at the decision timestamp.
- Detect duplicate, stale, contradictory, speculative, or low-reliability information before it affects a score.
- Produce probabilistic multi-horizon forecasts and measure their calibration over time.
- Evaluate the incremental contribution of each system layer with champion/challenger experiments.
- Preserve deterministic risk controls, broker idempotency, and safe degradation.
- Reconcile the real broker book on every scheduled invocation, including skipped decision sessions.
- Notify the owner immediately when safe mode, an abort, an AI fallback, or an unreconciled order occurs.
- Keep V2 operable on one local machine with SQLite initially.

### 3.2 Non-goals

- High-frequency trading, latency arbitrage, intraday market making, or tick-level prediction.
- Options, futures, short selling, leverage, crypto, or illiquid micro-cap trading.
- Letting an LLM select accounts, create arbitrary orders, or override risk limits.
- Treating model explanations as proof that a trade will work.
- Continuously retraining or self-modifying production strategy code.
- Mining social media or alternative data before core news, filings, and evaluation are reliable.
- Claiming investment performance from a small or statistically weak sample.

### 3.3 Operating assumptions

| Dimension | V2 assumption |
|---|---|
| Capital | $2,000 experimental account; no margin. |
| Universe | 40–100 highly liquid U.S. large-cap equities and broad/sector ETFs. |
| Cadence | One trading decision per valid session; optional 16:30 ET research precomputation and a 09:35–09:40 ET broker/decision cycle. |
| Information cutoff | Explicit per run; no document published after the cutoff may affect the decision. |
| Holding horizons | Primary forecasts at 5, 10, and 20 trading days; 1-day reaction is diagnostic; normal holdings approximately 2–20 days. |
| Direction | Long or cash only. |
| Execution | Fractional notional orders through Alpaca, initially paper only. |
| Compute | Local DeepSeek V4 Flash is the default model; cloud inference is disabled by default. |
| Benchmarking | SPY, cash, quant-only, and V1-style AI overlay. |

## 4. Design Principles and Invariants

1. **Evidence before opinion.** An AI assessment must cite stored evidence objects, not URLs or unsupported memory.
2. **Point-in-time correctness.** Every input has `published_at`, `retrieved_at`, and `available_at` timestamps. Evaluation uses only data available before the cutoff.
3. **Separate prediction from authority.** Models produce facts and forecasts; deterministic code decides eligibility, sizing, risk, and execution.
4. **Abstention is valid.** Missing, conflicted, stale, or weak evidence should result in a neutral AI contribution or no trade.
5. **One model is not a committee.** Multiple DeepSeek passes can enforce process separation, but they do not create truly independent opinions.
6. **Incremental deployment.** Every V2 capability can run in shadow beside V1 before influencing paper orders.
7. **Reproducibility.** A decision is reproducible from the code hash, config hash, data snapshot, prompt versions, model identifier, and stored outputs.
8. **Prospective evidence beats elaborate hindsight.** Historical event backtests are permitted only when timing and source coverage are trustworthy.
9. **Complexity must earn its place.** A feature or model stage is promoted only if it improves a preregistered metric without unacceptable operational cost.
10. **The broker book is reality.** Position count, sector exposure, cash, and risk are calculated from reconciled holdings plus pending orders—not from the desired proposal alone.
11. **Reconcile before returning.** A holiday, duplicate-session decision, or other research skip must never bypass broker reconciliation and health reporting.
12. **A silent safe mode is a failed control.** Safety events must block exposure and immediately notify the owner.

## 5. High-Level Architecture

```text
Official calendar + Market data + News + SEC filings + Macro data
                              |
                              v
                 Raw point-in-time document store
                              |
                              v
            Normalization, entity resolution, validation
                              |
                   +----------+----------+
                   |                     |
                   v                     v
          Quant feature store       Canonical event graph
                   |                     |
                   |             Multi-pass AI research
                   |                     |
                   +----------+----------+
                              v
                 Multi-horizon forecast records
                              |
                              v
                 Calibration and signal fusion
                              |
                              v
              Portfolio proposal + scenario checks
                              |
                              v
                  Deterministic risk engine
                              |
                              v
                    Broker execution adapter
                              |
                              v
             Reconciliation, outcomes, and evaluation
```

### 5.1 Service boundaries

V2 remains a scheduled modular monolith. Components are Python modules and durable database tables, not network services. This keeps deployment simple while preserving interfaces that can later be separated.

| Component | Responsibility | Authority |
|---|---|---|
| Trading-calendar service | Determines sessions, early closes, and decision cutoffs. | May permit or skip a run; cannot trade. |
| Source adapters | Fetch raw data and preserve provider metadata. | Cannot score or trade. |
| Point-in-time store | Records raw payloads, timestamps, hashes, and revisions. | Immutable append plus explicit correction records. |
| Entity/event engine | Maps documents to securities and canonical real-world events. | Cannot produce orders. |
| Feature engine | Computes versioned deterministic features. | Cannot call broker. |
| AI research pipeline | Extracts facts, checks evidence, analyzes impact, and forecasts. | Produces validated research objects only. |
| Calibration service | Maps raw forecasts to empirical probabilities/expected returns. | Uses completed historical outcomes only. |
| Strategy/portfolio engine | Combines forecasts and proposes target weights. | Proposal only. |
| Risk engine | Reduces or rejects targets and orders. | Final pre-trade authority. |
| Broker adapter | Submits only validated order intents. | Cannot invent a symbol, side, or quantity. |
| Evaluator/observer | Reconciles state, scores outcomes, reports health. | May trigger safe mode; cannot add exposure. |

### 5.2 Daily schedule and stage ordering

V2 separates slow research preparation from the execution-critical morning path while preserving one trading decision per valid session.

```text
16:30 ET after session T closes
  freeze completed bars -> compute quant features -> ingest/process filings and news

09:35 ET on session T+1
  reconcile broker first -> ingest overnight delta -> verify material events -> synthesize forecasts

after 09:40 ET, when research is complete
  refresh quote/account -> construct portfolio -> apply risk -> submit marketable limits

after order window and next invocation
  reconcile fills -> score health -> notify/report
```

Rules:

- Paper/live orders may be submitted only during regular market hours; never queue the normal strategy after the close.
- Reconciliation runs before any “already decided,” holiday, or skipped-session return path.
- If the prior session was already decided, the invocation becomes `reconcile_only` and still writes a current-date report and health summary.
- Research prepared at 16:30 is immutable and cacheable. The morning stage processes only overnight deltas and material updates.
- The morning decision-to-order target is under three minutes, with a configurable hard deadline. Missing the deadline results in no new exposure.

## 6. Data Sources and Point-in-Time Model

### 6.1 Source priority

| Data category | Primary source | Secondary/fallback | V2 use |
|---|---|---|---|
| Trading calendar | Alpaca calendar / official exchange calendar | Local cached calendar | Session validity, early close, cutoffs |
| Daily/intraday prices | Alpaca market data | Current yfinance adapter | Features, fresh price, liquidity, execution checks |
| Quotes and trades | Alpaca IEX initially | None | Spread and freshness checks |
| Company news | Alpaca News/Benzinga where available | Yahoo headlines | Discovery and secondary confirmation; analyst notes are low-priority by default |
| Regulatory filings | SEC EDGAR submissions, filing documents, and XBRL APIs | Company investor-relations release | Primary actionable source for 8-K, 10-Q, 10-K, guidance, and material facts |
| Macro data | FRED for current observations; ALFRED vintages for research | Cached last-known valid observation | Rates, inflation, labor, financial conditions |
| Corporate metadata | SEC/company/broker metadata | Versioned manual mapping | CIK, ticker, sector, industry, aliases |

The free Alpaca IEX feed is appropriate for initial testing but represents one exchange rather than the consolidated U.S. market. V2 records the feed identifier and does not treat IEX liquidity as full-market liquidity. A paid consolidated SIP feed is a later operational upgrade, not a prerequisite for shadow research.

### 6.2 Required timestamps

Every market observation or document must include:

- `event_time`: when the underlying event occurred, if known.
- `published_at`: source publication timestamp.
- `retrieved_at`: when Sea Lion received it.
- `available_at`: earliest defensible time the system could have used it.
- `effective_date`: business or reporting date represented by the value.
- `revision_id`: provider revision or content hash.

For live decisions, `available_at` is normally the retrieval timestamp unless a trusted streaming timestamp proves earlier availability. For backtests, it is the historical publication or vintage timestamp—not the final revised value.

### 6.3 Raw-data rules

- Store the original provider payload or a lossless permitted subset before transformation.
- Store full article text only when the provider and license permit it; otherwise store title, summary, metadata, URL, and extracted facts.
- Hash content to detect exact duplicates and later edits.
- Do not overwrite a corrected filing, news item, or macro observation. Link the new revision to the old one.
- Assign explicit source quality, coverage, and licensing flags.
- Quarantine malformed, future-dated, or implausibly timestamped data.
- Retain the current yfinance source as fallback during migration, but flag fallback-only decisions.

## 7. Comprehensive News and Event Pipeline

This is the largest functional change in V2. The unit of reasoning becomes a **canonical event**, not an isolated headline.

### 7.1 Processing stages

1. **Collect** new documents from Alpaca News, Yahoo fallback, and SEC EDGAR using a watermark per source. Perform a broad after-close collection and a small overnight-delta collection before the morning decision.
2. **Normalize** text encoding, times, source names, URLs, tickers, and document types.
3. **Resolve entities** using ticker, company name, SEC CIK, product aliases, subsidiaries, and named competitors.
4. **Deduplicate documents** by exact hash, normalized headline similarity, URL canonicalization, and near-duplicate embeddings or fingerprints.
5. **Extract atomic claims** with evidence spans, dates, quantities, actors, event types, and uncertainty.
6. **Cluster globally** into a canonical event across sources, symbols, batches, and daily runs.
7. **Assess novelty** relative to prior events and the market's likely prior knowledge.
8. **Verify** material claims against primary sources or independent reports where available.
9. **Detect contradictions** between sources, within a filing, or between a headline and full content.
10. **Estimate impact** for affected symbols, sector peers, and broad-market exposures.
11. **Synthesize** a bounded forecast with evidence, uncertainty, horizon, and invalidation conditions.
12. **Score outcomes** after 5, 10, and 20 trading days for calibration, with an optional 1-day reaction diagnostic.

### 7.2 Event routing policy

The first-week event distribution showed that generic macro and analyst commentary can crowd out scarce company-specific evidence. V2 applies routing before the expensive analysis passes:

| Event class | Default treatment |
|---|---|
| SEC 8-K, earnings release, or guidance change | Highest priority; eligible for evidence audit and main-tier analysis. |
| SEC 10-Q/10-K material change | High priority when novelty extraction identifies a meaningful change. |
| Merger, litigation, regulatory, executive, or capital-allocation event | Analyze when materiality and entity mapping pass. |
| Company-specific reputable news | Analyze when novel and supported by content rather than headline alone. |
| Analyst rating or price-target change | Low priority; require corroborating company or price/volume evidence. |
| Generic macro commentary | Exclude from company main-tier context unless importance is at least 0.70 and a causal company/sector channel is identified. |
| Re-syndicated or cross-day duplicate | Link to the existing canonical event; do not consume another main-tier slot. |
| Rumor or unattributed speculation | Store for audit but keep non-actionable by default. |

The main-tier candidate budget reserves capacity for primary company events. A flood of macro headlines cannot displace a material filing or earnings update.

### 7.3 Canonical event example

```json
{
  "event_id": "evt_20260904_acme_guidance_01",
  "event_type": "guidance_revision",
  "primary_symbol": "ACME",
  "related_symbols": ["PEER1", "PEER2"],
  "event_time": "2026-09-04T12:30:00Z",
  "available_at": "2026-09-04T12:31:08Z",
  "status": "verified",
  "novelty": 0.86,
  "source_quality": 0.94,
  "contradiction_level": 0.08,
  "claim_ids": ["claim_101", "claim_102"],
  "document_ids": ["sec_8k_123", "news_456"],
  "supersedes_event_id": null
}
```

### 7.4 Event quality gates

An event can affect the AI score only if:

- At least one source document is stored and available before the run cutoff.
- Every material factual claim references a document and evidence span or structured field.
- The primary entity mapping is unambiguous.
- The event is new or materially updates an older event.
- Source-quality and freshness thresholds pass.
- Contradictions are either resolved or represented as increased uncertainty.

Rumors and single-source speculative reports may be recorded, but they remain non-actionable unless explicitly enabled in a later experiment.

## 8. Local Model Routing and Multi-Pass Research

Both routing tiers may use the locally hosted DeepSeek V4 Flash model. In V2, “cheap” and “main” describe task depth and latency, not API price.

### 8.1 Model passes

| Pass | Input | Output | Scope |
|---|---|---|---|
| A. Extractor | Raw document or structured filing facts | Atomic claims, entities, quantities, dates, evidence spans | Every eligible document |
| B. Event builder | New claims plus candidate prior events | Canonical event assignment, novelty, update relationships | Every relevant claim |
| C. Evidence auditor | Canonical event and source packet | Supported/unsupported claims, contradictions, missing primary evidence | Material events only |
| D. Company analyst | Verified event, quant context, recent company history | Bull/bear mechanisms, horizon, affected metrics, forecast draft | Top candidates |
| E. Skeptical reviewer | Same evidence plus analyst draft | Alternative explanations, market-pricing argument, failure modes | Top candidates |
| F. Context analyst | Sector, peer, index, and macro context | Direct and second-order impact adjustments | When relevant |
| G. Synthesizer | Outputs C–F, no raw internet access | Final structured forecast and abstention decision | Top candidates |

Each pass receives only the information required for its role. It returns versioned JSON validated against a strict schema. The synthesizer sees disagreements but cannot delete them from the audit record.

Prompts must not reveal production acceptance thresholds such as `min_model_confidence`. The first-week cluster at exactly 0.60 demonstrates anchoring to the stated floor. The model instead reports its best estimate of `p_positive_excess_return` for each horizon, plus evidence quality and reasons to abstain; deterministic code applies thresholds after validation and calibration.

### 8.2 Why multiple passes usefully differ

Multiple passes improve procedural discipline: one pass extracts evidence, another challenges the interpretation, and a final pass resolves explicit disagreements. They do not eliminate correlated hallucination because the same model weights are used. V2 therefore relies on source verification and realized-outcome calibration—not “model voting”—for trust.

An optional second provider may later verify only high-impact, high-disagreement events. It is not required for V2 launch and must prove incremental value in a shadow experiment.

### 8.3 Context and token policy

Local tokens have no direct API charge, but they still create latency, memory pressure, and failure risk. V2 therefore uses:

- Retrieval of only the most relevant evidence spans plus structured facts.
- Stable system prompts and versioned task prompts.
- Separate context per pass to reduce anchoring.
- Maximum documents, evidence spans, and generated tokens per event.
- A wall-clock deadline for the complete daily run.
- Content-hash caching for deterministic extraction stages.
- Priority queues so filings and high-impact events finish before low-importance commentary.
- After-close precomputation for slow extraction and event clustering, leaving only overnight deltas and synthesis on the morning critical path.
- Per-stage latency metrics and deadlines; optional evaluation of a faster local model is a controlled experiment, not an automatic provider switch.

### 8.4 Prompt-injection boundary

News articles, filings, webpages, and provider metadata are untrusted data. They are enclosed in explicit data fields and never appended as instructions. Model outputs cannot change prompts, code, configuration, risk thresholds, broker endpoints, or tool permissions.

## 9. Quantitative Feature Layer

V2 retains the explainable V1 features and adds feature families gradually. Every feature is versioned, point-in-time, lagged appropriately, and accompanied by a missingness flag.

| Family | Candidate features | Purpose |
|---|---|---|
| Price momentum | 5/20/60/120-day return, skip-recent momentum | Multiple horizons and persistence |
| Relative strength | Return versus SPY and sector ETF | Separate stock-specific movement from market beta |
| Trend | Price versus 20/50/200-day averages, slope | Direction and regime persistence |
| Mean reversion | 1/3/5-day residual return and gap | Avoid buying short-lived overextension |
| Volatility | Realized volatility, downside volatility, range, gap risk | Sizing and risk state |
| Liquidity | Dollar volume, volume surprise, spread, quote freshness | Eligibility and execution cost |
| Market risk | Beta, correlation, sector exposure, drawdown | Portfolio-aware constraints |
| Fundamentals | Revenue/EPS growth, margin trend, leverage, cash flow, quality | Medium-horizon context |
| Valuation | Simple sector-relative multiples where point-in-time data is valid | Avoid extreme price/fundamental mismatch |
| Filings/events | Earnings surprise, guidance change, filing novelty, event age | Catalyst measurement |
| Macro/regime | Rates, curve, inflation, labor, volatility regime | Scale exposure and interpret sector impact |

Features enter a **feature registry** containing definition, owner, source, lookback, lag, missing-value behavior, first-valid date, and test coverage. No experimental feature silently becomes a production input.

## 10. Forecast Contract and Calibration

V2 replaces one uncalibrated impact/confidence pair with an explicit multi-horizon forecast. The primary holding-period horizons are 5, 10, and 20 trading days. A 1-day reaction metric may be logged for execution diagnostics, but it is not the primary confidence target.

```json
{
  "symbol": "MSFT",
  "decision_time": "2026-09-08T13:40:00Z",
  "forecast_version": "v2.0",
  "horizons": {
    "5d": {"expected_excess_return_bps": 55, "p_positive_excess_return": 0.61},
    "10d": {"expected_excess_return_bps": 72, "p_positive_excess_return": 0.59},
    "20d": {"expected_excess_return_bps": 95, "p_positive_excess_return": 0.58}
  },
  "evidence_quality": 0.88,
  "model_disagreement": 0.21,
  "catalyst_half_life_days": 6,
  "invalidation_conditions": ["guidance withdrawn"],
  "risk_flags": [],
  "evidence_ids": ["claim_101", "claim_102"],
  "abstain": false
}
```

### 10.1 Calibration rules

- Raw model confidence never directly sets position size.
- Store forecasts before outcomes are known; records are immutable.
- Calculate forward close-to-close and executable-price returns at each horizon.
- Compare security returns with SPY and sector benchmarks.
- Track probability calibration with Brier score and reliability buckets.
- Fit simple isotonic or logistic calibration only after a sufficient prospective sample.
- Maintain separate calibration by horizon and optionally by event class; do not create tiny regime buckets.
- Do not reveal production confidence floors in prompts.
- Begin empirical fitting only after at least 60 scored assessments, while retaining the full uncalibrated history for evaluation.
- Until calibration is stable, cap the AI overlay at the current 20%, require conservative evidence gates, and shrink probabilities toward 0.50.

## 11. Signal Fusion

V2 keeps quantitative and AI evidence separate until the final ensemble so their incremental contribution remains measurable.

```text
quant_score      = normalized deterministic cross-sectional score
event_score      = calibrated event forecast, shrunk toward zero
quality_multiplier = source_quality × freshness × calibration_factor
ai_overlay       = clamp(event_score × quality_multiplier, -ai_cap, +ai_cap)
combined_score   = quant_weight × quant_score + ai_weight × ai_overlay
```

Initial production weights remain:

- Quantitative score: 80%.
- AI event overlay: at most 20%.
- Negative AI evidence may reduce or veto a new long position even when it does not create a short.
- No actionable recent event means an AI contribution of zero, not a fabricated opinion.
- High contradiction, poor evidence quality, or large reviewer disagreement shrinks the AI contribution toward zero.
- Weight changes require a recorded experiment and prospective results; they are not tuned to one backtest.

Every run must record whether the AI actually changed the decision. Required attribution fields are:

- Change in each symbol's rank and ensemble score.
- Difference in selected membership between quant-only and AI-assisted portfolios.
- Sum of absolute target-weight differences.
- Number and notional value of orders caused, prevented, or resized by AI.
- Subsequent return contribution of those differences at 5, 10, and 20 sessions.

An AI layer that produces plausible prose but never changes rankings, weights, or risk decisions has no measured trading contribution. V2 will not raise the 20% cap merely to force more visible activity.

## 12. Portfolio Construction

For a $2,000 account, V2 should favor robust rules over a fragile optimizer.

1. Filter for tradability, freshness, minimum liquidity, and complete risk metadata.
2. Rank the eligible universe by the combined score.
3. Select at most ten securities, subject to rank hysteresis, sector limits, and correlation-cluster limits.
4. Start from inverse-volatility weights.
5. Apply score strength, forecast uncertainty, and event-risk multipliers.
6. Scale total exposure by the market regime.
7. Apply turnover-aware adjustment to existing holdings.
8. Pass target weights to the deterministic risk engine.

V2 may calculate a shrinkage covariance matrix for diagnostics and scenario analysis. A numerical optimizer should not control production weights until it outperforms the simple portfolio prospectively and remains stable under small input perturbations.

### 12.1 Rank hysteresis and replacement policy

The first week produced one boundary swap per session, even though the expected daily edge is smaller than the estimated round-trip cost. V2 separates **entry rank** from **retention rank**:

- A new holding normally enters only when ranked in the top 10 and all eligibility rules pass.
- An existing holding remains eligible while ranked in the top 15, or while its score remains within a configured band of the 10th-ranked candidate.
- Replace a retained holding only when the incoming candidate exceeds it by a minimum score margin after estimated round-trip cost.
- Trend failure, a hard risk flag, loss lock, stale data, or material negative evidence may still force reduction regardless of rank.
- Measure churn avoided, foregone return, turnover saved, and net effect prospectively.

The starting retention rank is 15. The score band and replacement margin are configuration values chosen before evaluation; they must not be retuned from a few sessions. The expected outcome is lower turnover, but the design does not assume a specific reduction until measured.

### 12.2 Scenario checks

Before order generation, estimate portfolio effects for at least:

- Broad market move of -3%.
- Technology/growth factor move of -5%.
- Rates shock affecting duration-sensitive sectors.
- Largest position gap of -10%.
- Two highly correlated positions moving together.
- Overnight adverse event before the next daily cycle.

These are deterministic exposure checks, not claims that the scenarios are complete.

## 13. Deterministic Risk Controls

The V1 controls remain the starting limits. V2 adds portfolio and evidence-quality controls.

| Control | Initial limit or rule | V2 behavior |
|---|---:|---|
| Gross exposure | 80% of equity | Keep at least 20% cash; reduce further in adverse regimes. |
| Single position | 10% of equity | Hard clamp; no model override. |
| Position count | Maximum 10 intended holdings | Count reconciled broker holdings plus pending entries; dust is separately flagged and swept. |
| Sector exposure | 25% of equity | Calculate from the actual book plus pending orders using versioned sector metadata. |
| Correlation cluster | 25% of equity | Group highly correlated or economically similar names. |
| Portfolio beta | Configured upper bound, initially near 1.0 | Scale new exposure when estimate exceeds limit. |
| Daily loss lock | 2% of start-of-day equity | Cancel open buys and block new exposure. |
| Drawdown safe mode | 10% from high-water mark | Block new exposure; liquidation remains manual/risk-reducing. |
| Daily turnover | 20% of equity | Scale non-risk-reducing orders. |
| Order notional | $10 minimum; 10% maximum | Avoid dust and oversizing. |
| Spread/liquidity | Configured maximum spread and minimum dollar volume | Skip rather than cross an abnormal market. |
| Earnings/event concentration | Maximum two simultaneous high-impact binary events | Reduce overlapping catalyst risk. |
| Evidence quality | Below threshold means neutral AI overlay | Quant-only behavior must be explicit in the run record. |
| Model disagreement | Above threshold means abstain or shrink | Never resolve uncertainty by increasing size. |
| Operational health | All critical checks green | Otherwise no new exposure. |
| Account leverage | Broker multiplier must equal 1 for live | `check-broker` and every live run fail closed if margin is enabled. |

Any automatic limit adjustment may only reduce risk. Raising a hard limit requires an explicit configuration change, review, tests, and a new config hash.

## 14. Broker Integration and Execution

V2 preserves the Alpaca adapter and introduces stronger market-state checks.

- Use separate paper and live credentials, endpoints, stores, and explicit modes.
- Query the exchange/broker calendar rather than assuming every weekday is tradable.
- Define broker order lifecycle states once in the broker interface. Persistence, reconciliation, reports, and tests must consume that same definition; Alpaca states such as `pending_new` must not be duplicated in partial lists.
- At every scheduled invocation, reconcile first and capture account equity, cash, buying power, multiplier, positions, open orders, and market status—even if the research/decision stage will be skipped.
- Immediately before submission, refresh price/quote, spread, position, buying power, and market status.
- Use deterministic `client_order_id` values and query by ID after ambiguous responses.
- Prefer marketable limit orders during regular hours with a bounded price offset and timeout.
- Do not chase partial fills. Recalculate exposure from confirmed fills.
- When a full exit would otherwise leave less than the minimum order value, submit an exact-quantity close-position intent rather than a price-derived partial quantity.
- Provide an idempotent `sweep-dust` maintenance command that lists candidates before closing them and records each approved cleanup.
- Reconcile broker versus local state after the order window and again at the next invocation. A skipped decision still has a `reconcile_only` stage.
- Reject paper/live strategy submission outside regular market hours. Overnight queued orders are not part of the normal V2 path.
- Require a cash account for live deployment. `check-broker` and the live preflight fail if Alpaca reports `multiplier > 1`.
- Permit risk-reducing actions in safe mode only under explicit deterministic rules.
- Keep live submission disabled until paper promotion gates pass.

Regression fixtures must include `pending_new`, `new`, `accepted`, `partially_filled`, `filled`, `expired`, `canceled`, `rejected`, and ambiguous responses. The test must prove that every open state is refreshed before expected positions are compared with the broker.

## 15. Backtesting and Prospective Evaluation

### 15.1 Quantitative backtest

Improve the V1 backtest with:

- Point-in-time universe membership where obtainable.
- Delisted securities or an explicit survivorship-bias limitation.
- Split/dividend-consistent prices.
- Conservative spread, slippage, and fill assumptions.
- Separate execution scenarios for the supported morning path and an intentionally adverse overnight-queue comparison. Seed the morning model with the observed approximately 4–5 bps slippage; model overnight expiry around the observed 2 of 10 orders only as a sensitivity case, not as a durable estimate.
- Walk-forward parameter selection with untouched evaluation periods.
- Feature and configuration versions recorded per result.
- Sensitivity tests around lookbacks, thresholds, and rebalancing assumptions.

### 15.2 AI/event evaluation

Alpaca documents historical news coverage back to 2015, but a credible AI backtest also requires precise availability times, consistent coverage, licensed content, stable prompts, and awareness that the current model did not exist historically. V2 therefore uses historical news mainly for pipeline validation and exploratory research. Promotion decisions depend primarily on frozen prospective forecasts from shadow and paper sessions.

### 15.3 Champion/challenger experiment

Every eligible daily run generates three portfolios from the same data cutoff and execution assumptions:

| Arm | Description | Purpose |
|---|---|---|
| A | Quant-only | Stable benchmark |
| B | V1 headline AI overlay | Measure value of the existing method |
| C | V2 verified-event AI overlay | Measure incremental value of V2 research |

Only one arm may send orders. The others remain shadow portfolios with simulated fills. Arm selection is configured before the run and cannot change based on that day's outputs.

### 15.4 Evaluation metrics

| Category | Metrics |
|---|---|
| Forecast | Rank IC, directional accuracy, Brier score, calibration slope, error by horizon |
| Portfolio | Net/excess return, volatility, drawdown, Sharpe, turnover, exposure, hit rate |
| Incremental AI | Arm C minus A and C minus B, before and after estimated costs |
| Event quality | Precision of relevance/entity mapping, duplicate-cluster accuracy, contradiction rate |
| Operations | Run success, stage latency, source failures, schema failures, broker reconciliation errors, alert delivery |
| Decision divergence | Rank changes, membership changes, target-weight distance, AI-caused orders, net AI contribution |

Report confidence intervals or bootstrap ranges when possible. Label samples below the preregistered minimum as exploratory.

### 15.5 Initial promotion gates

V2 may influence paper orders only after:

- At least 10 consecutive trading sessions complete without a critical data, duplication, or reproducibility defect.
- Skipped and holiday invocations successfully execute reconciliation and create a current-date health report.
- Every broker lifecycle state in the adapter is covered by reconciliation regression tests.
- Safe mode, abort, AI fallback, and unreconciled-order tests produce a verified external notification.
- Position-count and sector limits are proven against actual holdings plus pending orders, including a dust-position fixture.
- At least 95% of material event forecasts contain valid evidence references.
- Identical stored inputs reproduce deterministic features and validated structured outputs within the documented model limitations.
- Every risk and broker failure path passes automated tests.
- V2 produces no unexplained violation of cutoff timestamps.

V2 may become the selected paper champion after at least 30 trading sessions and 60 scored security forecasts, provided it is operationally stable and does not materially underperform the quant-only arm on risk-adjusted, cost-aware measures. The review must explicitly report realized versus target exposure and AI-versus-quant divergence; apparent outperformance caused by holding more cash is not attributed to forecast skill. This gate is deliberately a minimum operational checkpoint, not proof of statistical significance.

No live-capital promotion should be based on fewer than 30 additional paper sessions, and live deployment still requires manual approval.

## 16. Storage and Audit Schema

V2 adds append-only tables or equivalent records:

| Record | Key fields |
|---|---|
| `source_documents` | Source, provider ID, raw hash, title/content reference, timestamps, license flags |
| `document_revisions` | Prior/new hashes, revision time, relationship |
| `entity_mentions` | Document, text span, entity ID, symbol, CIK, confidence |
| `atomic_claims` | Claim text/structure, evidence span, units, time, certainty |
| `canonical_events` | Type, status, novelty, source quality, contradiction, related symbols |
| `event_claim_links` | Event-to-claim relationship and support/contradiction role |
| `feature_snapshots` | Symbol, decision cutoff, feature version, values, missingness |
| `model_passes` | Pass type, input hash, prompt/schema/model versions, output, latency, validation |
| `forecasts` | Symbol, horizons, raw/calibrated values, evidence, abstention, frozen time |
| `portfolio_arms` | Arm, target weights, constraints, simulated execution assumptions |
| `forecast_outcomes` | Horizon, realized and benchmark returns, scoring status |
| `calibration_models` | Training window, version, parameters, validation metrics |
| `health_events` | Severity, component, reason, remediation, acknowledgement |
| `broker_snapshots` | Actual positions, cash, equity, multiplier, open orders, source time |
| `decision_divergence` | Quant/AI rank, membership, weight, order, and return differences |
| `notification_attempts` | Health event, channel, destination hash, delivery status, retry count |

Large raw documents may live in compressed files referenced from SQLite. The database remains the authoritative index and stores hashes that detect missing or changed files.

## 17. Failure and Degradation Policy

| Failure | Default V2 response |
|---|---|
| Primary news source fails | Use approved fallback, mark degraded provenance, and shrink or neutralize AI overlay. |
| SEC or macro source fails | Use only previously retrieved point-in-time data that remains valid; do not imply freshness. |
| Market price/quote is stale | Block new orders. |
| Document is malformed or timestamp is uncertain | Quarantine it; it cannot affect the decision. |
| Extractor/model output is invalid | Retry once with repair instructions; then mark the item failed. |
| Evidence auditor finds unsupported material claims | Remove those claims and rerun synthesis or abstain. |
| Analyst and skeptic strongly disagree | Shrink toward neutral or abstain. |
| Model endpoint is unavailable | Record quant-only shadow output; paper/live new exposure remains disabled unless explicitly configured. |
| Daily run exceeds deadline | Stop remaining low-priority research, finalize only complete candidates, or make no new trades. |
| Broker response is ambiguous | Query existing order by deterministic ID; never submit a blind duplicate. |
| Broker/local state disagrees | Enter safe mode and block new exposure. |
| A decision session is skipped or already completed | Run reconciliation, emit health summary, and write a report under the invocation date. |
| Safe mode, abort, or AI fallback occurs | Persist the event, send an external notification, and return a non-zero/attention exit status where appropriate. |
| Notification delivery fails | Persist failure, retry through the configured fallback, and make the failure visible in the next report. |
| Database or audit write fails | Abort before any broker submission. |

The system must never convert missing information into false certainty. Every degraded run states what was unavailable and whether orders were blocked.

## 18. Security and Privacy

- Keep credentials in a local `.env` or OS-backed secret store excluded from Git.
- Never log API secrets, full account identifiers, or authentication headers.
- Use distinct Alpaca paper and live keys; verify the endpoint matches the configured mode.
- Restrict file permissions for secrets, databases, and raw licensed content.
- Allowlist outbound data and broker hosts.
- Do not expose broker credentials to model prompts or model-serving processes.
- Sanitize untrusted documents and enforce instruction/data separation.
- Pin prompt/schema/model identifiers and log changes.
- Protect local model and dashboard endpoints from external network access unless authentication is added.
- Maintain a kill switch that cancels open orders and blocks new exposure without automatically liquidating positions.
- Back up configuration and audit records; test restoration before live use.

## 19. Observability and Daily Report

The daily report should be useful even when no trade occurs.

### 19.1 Required sections

- Run mode, decision cutoff, code/config/model versions, and health status.
- Source coverage, freshness, failures, documents ingested, and canonical events created/updated.
- Material events with supported claims, contradictions, and source quality.
- Quant-only, V1 AI, and V2 AI rankings side by side.
- Forecasts at 5/10/20 days with evidence and abstentions.
- Quant-versus-AI rank, membership, target-weight, order-notional, and realized-return differences.
- Proposed targets, risk reductions/rejections, scenario exposures, and actual-book position/sector exposure.
- Orders, acknowledgements, fills, slippage, and reconciliation.
- Prior forecasts reaching an evaluation horizon and their scores.
- Compute latency, cache hit rate, token counts, and optional estimated electricity/cloud cost.
- Plain equity-curve and realized-versus-target-exposure charts, always labeled with the small-sample session count.

Report filenames use the invocation date, not the prior market-data date. The report separately displays `run_date`, `decision_as_of`, and `run_outcome` (`completed`, `reconcile_only`, `skipped`, `aborted`, or `safe_mode`).

### 19.2 Alerts

Alert only on actionable conditions:

- A scheduled valid-session run did not start or finish.
- Critical data is stale or unavailable.
- Cutoff or point-in-time validation fails.
- Broker submission is ambiguous, rejected, or unreconciled.
- A hard risk limit activates.
- Local model endpoint remains unavailable after retry.
- Disk space, database integrity, or audit persistence is unsafe.
- Paper/live configuration or credential environment appears mismatched.

The minimum implementation has two layers:

1. `run_daily.sh` prints a single machine-readable and human-readable summary containing `status`, `safe_mode`, `n_orders`, `ai_available`, and `reconciliation_status`. Attention states exit non-zero so the scheduler can detect them.
2. A notification hook sends email to the configured operational address, initially `kevinxsn@outlook.com`, for safe mode, non-holiday aborts, AI fallback in paper/live, ambiguous/unreconciled orders, and repeated source failure.

Email addresses remain configuration, not hard-coded strategy logic. Notification delivery is tested with a fake adapter before real email is enabled. Successful uneventful runs do not need an email.

## 20. Compute and Cost Budget

Local inference changes the budget from dollars per token to bounded resource use.

| Resource | V2 starting policy |
|---|---|
| Local-model token spend | $0 API cost; still record input/output tokens per pass. |
| After-close research wall time | Target under 15 minutes; it is outside the order-critical path. |
| Morning decision-to-order time | Target under 3 minutes and hard limit under 10 minutes. |
| Parallelism | Bounded to protect machine responsiveness and avoid endpoint failures. |
| Context | Evidence retrieval plus per-pass caps; no unbounded full-corpus prompts. |
| Cache | Content hash + prompt/schema/model version. |
| Cloud fallback | Disabled by default; explicit opt-in and monthly hard cap required. |
| Paid market/news data | $0 initially; evaluate only after V2 proves useful in paper. |
| Storage | Compress raw documents; set retention and licensing rules. |

The local model's marginal token price should not justify analyzing irrelevant information. Every pass must have a measurable purpose, and latency must not compromise the decision cutoff or broker checks.

## 21. Implementation Plan

Implementation should proceed behind feature flags so the current V1 daily cycle remains usable.

### Phase 0 — Freeze the measured V1 baseline (1 day)

- Tag the code/config/prompt state used for the September 4–11 paper review.
- Confirm every run records a real Git commit hash.
- Freeze quant-only and V1 headline experiment definitions.
- Preserve the reviewed database/reports and add reproducible fixtures from the actual paper runs.
- Record the first-week metric baseline, including the false safe-mode incident, separately from future clean sessions.

**Exit:** The same fixture produces the same deterministic features, portfolio proposal, and risk decisions.

### Phase 1 — V1.1 reliability hardening (2–3 days; release blocker)

- Verify the reported shared open-status fix is deployed and covers Alpaca `pending_new`.
- Verify reconciliation occurs before every skip/early return and records `reconcile_only`.
- Verify exact-quantity exits prevent new dust positions; add the idempotent dust-sweep maintenance path.
- Calculate position count, cash, and sector exposure from the reconciled book plus pending orders.
- Add paper/live account-multiplier preflight; live requires `multiplier == 1`.
- Add an end-to-end regression fixture for the September 9–10 false-safe-mode sequence.
- Preserve manual safe-mode clearing. Never make a regression fix silently clear an existing safe state.

**Exit:** The reviewed incident replays cleanly; every open order is refreshed, expected and broker positions match, and skipped sessions still reconcile.

### Phase 2 — Alerts, reporting, and decision measurement (2–4 days; release blocker)

- Add the one-line run summary and attention exit statuses.
- Add the notification hook and verify delivery to the configured owner address.
- Add actual-versus-target exposure and book-level sector exposure.
- Add equity and exposure charts and current-date filenames for skipped runs.
- Add AI-versus-quant rank, membership, weight, order, and realized-return divergence.
- Implement rank hysteresis behind a feature flag and run it as a shadow challenger.

**Exit:** Tested safe-mode, abort, AI-fallback, and reconciliation failures generate external alerts; all completed/skip outcomes are distinguishable without opening the database.

### Phase 3 — Point-in-time sources and staged scheduling (3–5 days)

- Add trading-calendar validation.
- Implement Alpaca News and SEC EDGAR adapters.
- Add FRED/ALFRED adapter for a small macro series set.
- Add raw-document, timestamp, hash, revision, and source-quality records.
- Preserve yfinance as an explicitly labeled fallback.
- Add the after-close research job and morning delta/decision job with separate health records.

**Exit:** A replay proves that no post-cutoff document enters a decision, and cached after-close research shortens the morning critical path without using stale evidence.

### Phase 4 — Canonical event pipeline (4–7 days)

- Add entity registry and CIK/ticker/alias mapping.
- Implement document deduplication and cross-run event clustering.
- Add atomic-claim extraction and evidence spans.
- Add novelty, contradiction, and update/supersession handling.
- Reserve main-tier capacity for filings and material company events; gate generic macro and analyst commentary.

**Exit:** A labeled fixture set demonstrates acceptable entity mapping and event clustering, with every material claim linked to evidence.

### Phase 5 — Multi-pass AI research (3–5 days)

- Implement extractor, evidence-auditor, analyst, skeptic, context, and synthesizer schemas.
- Add independent pass contexts, deadlines, retries, validation, and caching.
- Add the multi-horizon forecast contract and abstention rules.
- Remove production confidence thresholds from prompts and emit sign probabilities instead.

**Exit:** Unsupported or contradictory inputs reliably shrink to neutral or abstain, and no model output can create an order.

### Phase 6 — Feature and portfolio upgrades (4–7 days)

- Add relative strength, residual returns, spread/liquidity, beta/correlation, and event features.
- Add a minimal, point-in-time SEC fundamentals set.
- Add sector/correlation cluster and scenario exposure checks.
- Keep the simple inverse-volatility portfolio as production default.
- Compare top-10/top-15 hysteresis with the V1 daily replacement policy after estimated round-trip cost.

**Exit:** All new features have registry entries, leakage tests, missing-data behavior, and shadow comparisons.

### Phase 7 — Outcomes, calibration, and experiments (3–5 days)

- Freeze forecasts and score primary 5/10/20-day outcomes automatically; retain 1-day reaction as a diagnostic.
- Create A/B/C portfolio arms and simulated execution.
- Add calibration, incremental-contribution, and operational metrics.
- Produce the V2 daily report.

**Exit:** Every forecast reaches an outcome record or a documented exception, and all arms are comparable from the same cutoff.

### Phase 8 — Shadow and Alpaca paper rollout (calendar time)

- Run V2 in shadow for at least 10 clean market sessions.
- Resolve cutoff, duplication, evidence, and reproducibility defects.
- Select one predeclared paper arm and run for at least 30 sessions.
- Review performance together with realized exposure, turnover, and AI intervention metrics; do not auto-promote.

**Exit:** Manual review approves continuation, revision, or rejection of V2. Live mode remains separately gated.

## 22. Acceptance Criteria

V2 is complete as a system release when:

- The existing V1 modes and deterministic risk behavior remain functional.
- Broker reconciliation runs before every decision skip or early return and recognizes every adapter-defined open status.
- Safe mode, non-holiday aborts, AI fallback, and unreconciled orders generate verified external alerts.
- Risk reports and limits use actual reconciled holdings plus pending orders, not proposal membership alone.
- Full exits do not create avoidable dust; residual dust is visible and has an audited cleanup path.
- Paper/live execution is restricted to regular market hours and live mode requires a non-margin account.
- Every actionable event has complete provenance and evidence references.
- Same-event stories are clustered across sources and daily runs.
- Contradictory or unsupported evidence cannot silently increase confidence.
- Forecasts are frozen before outcomes and scored at all configured horizons.
- Quant-only, V1 AI, and V2 AI arms run from the same decision cutoff.
- Every run states whether AI changed rank, membership, target weight, order notional, or realized return.
- Rank hysteresis is evaluated after costs before it can replace the V1 portfolio rule.
- Point-in-time validation, schema validation, risk tests, and broker-idempotency tests pass.
- A failed model or noncritical information source causes a documented safe degradation.
- A stale market feed, failed audit write, unsafe broker state, or critical health failure blocks new exposure.
- Daily reports show equity, realized/target exposure, book-level sector exposure, and why a security was selected, retained, replaced, rejected, sized, ordered, or skipped.

## 23. Deferred Decisions

- Paid SIP market data versus free IEX after paper evidence exists.
- A second independent model for rare high-impact verification.
- Vector database adoption; SQLite plus fingerprints should be tested first.
- Intraday event-triggered cycles; daily operation should become reliable first.
- Larger or dynamic universe construction.
- Formal optimizer, factor-neutral portfolio, tax-lot optimization, options, or shorting.
- Social sentiment, analyst estimates, transcripts, and premium fundamentals.
- Distributed deployment or managed cloud scheduling.

## 24. First-Week Feedback Traceability

This table ensures the September 4–11 paper review remains connected to implementation rather than becoming a detached retrospective. “Reported fixed” means the review states that code/tests were added; V2 still requires deployment verification and incident replay.

| Review finding | V2.1 treatment | Primary section | Status entering V2 |
|---|---|---:|---|
| P1: `pending_new` omitted, causing false safe mode | One authoritative lifecycle-state set; full-state regression fixture; reconcile before comparison | 14, 21 | Reported fixed; verify deployment |
| P2: no alerts for two days | One-line summary, attention exit status, email notification and delivery audit | 19, 21 | New release blocker |
| P3: skipped runs bypassed reconciliation | Reconcile before early return; explicit `reconcile_only` run | 5, 14, 17 | Reported fixed; verify deployment |
| P4: price-derived exit left ABBV dust | Exact-quantity close intent plus audited dust sweep | 14, 21 | Reported fixed; cleanup path pending |
| P5: overnight orders expired on gaps | Prohibit normal after-close paper/live submission; retain morning marketable limits | 5, 14 | Confirmed design choice |
| P6: paper account became cash-only | Require `multiplier == 1` for live preflight and runs | 13, 14 | Add automated check |
| Rank-boundary churn | Top-10 entry/top-15 retention hysteresis and replacement-cost margin | 12 | Shadow experiment |
| Confidence anchored at 0.60 | Hide threshold; forecast sign probability; calibrate after 60 assessments | 8, 10 | Schema/prompt change |
| AI changed no decisions | Record rank, membership, weight, order, and outcome divergence | 11, 15, 19 | Measurement required now |
| Macro-heavy, shallow event mix | SEC/company-event priority; macro ≥0.70 gate; cross-day deduplication | 6, 7 | V2 data priority |
| AI consumed approximately 95% of runtime | After-close precompute; morning delta path; three-minute target | 5, 8, 20 | Architecture change |
| Risk counted proposal rather than actual book | Use broker holdings plus pending orders for position and sector limits | 4, 13, 16 | Release blocker |
| Skipped reports used old session dates | Filename by invocation date; display separate decision `as_of` | 19 | Reporting fix |
| Initial deployment ramps slowly under 20% turnover | Preserve for $2,000 pilot; make any larger-account allowance explicit and separate | 13 | No V2 limit increase |
| Backtest execution assumptions need reality check | Morning 4–5 bps seed plus adverse overnight-expiry sensitivity | 15 | Evaluation update |
| Performance needs visual exposure context | Add equity curve and realized-versus-target exposure chart | 19 | Reporting update |

## 25. External References

- [Alpaca Historical News Data](https://docs.alpaca.markets/docs/historical-news-data)
- [Alpaca News API Reference](https://docs.alpaca.markets/reference/news-3)
- [Alpaca Historical Stock Data](https://docs.alpaca.markets/docs/historical-stock-data-1)
- [Alpaca Market Data FAQ](https://docs.alpaca.markets/docs/market-data-faq)
- [Alpaca Calendar API](https://docs.alpaca.markets/reference/calendar-2)
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [FRED API](https://fred.stlouisfed.org/docs/api/fred/)
- [NYSE Trading Hours and Calendars](https://www.nyse.com/markets/hours-calendars)

---

**Review note:** V2.1 is intentionally comprehensive in evidence handling but conservative in trading authority. The local model may read and analyze much more information; it still cannot bypass a cutoff, invent a fact, select an unrestricted asset, exceed a hard risk limit, or submit an order directly. Operational correctness and alerting are prerequisites for smarter analysis, not parallel nice-to-have work.
