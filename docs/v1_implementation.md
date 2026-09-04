# V1 implementation record

What was built against [the V1 design](ai_quant_trading_system_v1_design.md), section by section, and where I deliberately deviated and why. Written 2026-09-04.

## 1. Section-by-section status

| Design section | Status | Where |
|---|---|---|
| §3 Architecture: one repo, one DB, one worker, one broker adapter, immutable stage records | Done | `pipeline.py`, `store.py` |
| §4 Daily data flow (ingest → validate → features → AI → score → risk → execute → reconcile → evaluate) | Done; reconcile runs at the *start* of the next run (see 2.4) | `pipeline.py` |
| §5 Two-tier model routing, JSON-only contract, cache by input hash, retry-once-then-neutral, 20% cap, quant-only score observable, no broker authority | Done | `ai/` |
| §6 Signals: 20d/60d momentum excluding last day, price vs MA50, 20d realized vol, dollar volume + volume ratio, SPY regime, AI event score | Done | `features.py`, `strategy.py` |
| §7 Portfolio + every risk control in the table | Done, each with a unit test | `risk.py`, `tests/test_risk.py` |
| §8 Alpaca adapter, fresh position snapshot, marketable limit orders, deterministic `client_order_id`, pre-submit re-checks, reconciliation → safe mode | Done; Alpaca path is written but **not yet exercised** (no keys) | `broker/alpaca.py`, `execution.py`, `reconcile.py` |
| §9 Failure handling table | Done | see 3 below |
| §10 Cost budget with hard caps and 80% soft stop | Done | `ai/router.py` |
| §11 Backtest / shadow / paper / live rollout gates | Backtest + shadow + sim done; paper needs keys; live gate enforced in code | `backtest.py`, `pipeline._live_gate` |
| §12 Logs and metrics | Done: run trace, model trace, trading trace, daily HTML/JSON, metrics | `store.py`, `report.py` |
| §13 Security controls | Done: env-only secrets, redacting logger, separate paper/live creds+DBs, allowlists for providers/hosts/order types/symbols, kill switch, no auto-liquidation | `config.py`, `logging_setup.py`, `execution.kill_switch` |
| §15 Acceptance criteria | See 4 below | `tests/` |

## 2. Deviations and judgment calls

### 2.1 Added a `sim` mode with a local fill simulator (not in the design)
The design goes straight from shadow (no orders) to Alpaca paper. That leaves the execution, reconciliation, idempotency and safe-mode code untested until broker keys exist. `broker/sim.py` accepts orders after the close and fills them at the **next session's open** with configurable slippage, if the open is within the limit; otherwise the order expires unfilled (no chasing). The same class powers the backtest. This is the current phase: the whole loop runs daily with no account and no money.

### 2.2 The local LLM is both tiers by default; Anthropic is optional
You already run DeepSeek-V4-Flash (llama.cpp, port 8324) and Qwen3.6-27B (vLLM, port 8321) on the lab A100s. Both speak the OpenAI-compatible API, so `ai.cheap` and `ai.main` point at `${SEA_LION_LOCAL_LLM_URL}` and cost $0. The router still meters tokens and enforces the budget table, so switching `ai.main.provider: anthropic` (model `claude-opus-5`, via the official SDK with a JSON-schema constrained output) needs no code change. Measured: one cheap-tier extraction batch takes ~15 s on V4-Flash; the injection test ("IGNORE PREVIOUS INSTRUCTIONS…") was ignored by the model and would have been caught by validation anyway.

The design's "cheap model" was meant to be a cheap *cloud* model; here "cheap" just means the batch-extraction tier. If the local server is down, the router marks the AI layer unavailable, the proposal becomes quant-only, and orders are allowed only in the modes listed in `ai.quant_only_orders_allowed_modes` (`sim`, `paper`; never `live`), exactly the §5 fallback rule.

### 2.3 Free data: Yahoo Finance for bars *and* headlines
No key needed, which lets shadow/sim run today. `data.provider: alpaca` and `events_provider: alpaca` switch to Alpaca's IEX feed and news API once paper keys exist (paper accounts get both for free). Yahoo headlines are lower quality than a curated feed; that is acceptable for V1 because the AI contribution is capped and evaluated prospectively. A `file` event source exists for hand-approved events and tests.

### 2.4 Reconciliation happens at the start of the next run, not right after submission
Orders are placed after the close for the next session, so fills cannot be confirmed in the same run. Each run therefore begins by refreshing every locally-open order from the broker, cancelling anything older than `order_timeout_sec`, and comparing broker positions with what our own fill history implies. Any mismatch → safe mode. After a clean reconcile the broker snapshot becomes the baseline. The sim broker's next-open settlement is triggered in the same place.

### 2.5 Three gate levels in the risk engine instead of one "block"
The design's table mixes "block new exposure", "cancel buys", and "liquidation requires manual approval". I made this explicit:
- **no orders** (stale data, market closed, bad account): nothing is generated.
- **safe mode / drawdown ≥10%**: buys blocked; risk-reducing sells are computed but **held for manual review** (`held_for_review` in the risk artifact and report), never auto-submitted. Untouched holdings stay.
- **daily-loss lock (≤ −2% vs start-of-day)**: buys blocked; sells still submitted.

### 2.6 Turnover cap scales buys only
"Scale non-risk-reducing trades" is implemented literally: sells are never scaled; buys get whatever room remains under 20% of equity after sells. A large exit can therefore exceed 20% turnover on its own, which is the intended risk-reducing behavior.

### 2.7 Decision date collapses onto the latest completed session
Running on a weekend or holiday (or a cron misfire) resolves to the last bar date, but only if that bar is within `data.max_staleness_days`; otherwise the run aborts as stale before any model call or order. A second run for a session that already completed is skipped unless `--force`, so a cron retry cannot double-trade.

### 2.8 Replay is exact because inputs are frozen to parquet
`bars` in SQLite get overwritten by later fetches (Yahoo re-adjusts history after dividends). So the ingest stage also writes the exact frame to `runtime/data/snapshots/<run_id>.parquet` and records its hash; replay reads that file and consults only the model cache. The end-to-end test asserts the replayed proposal is identical while a *different* market-data source is wired in and the model provider is never called.

### 2.9 Quantities instead of notional on Alpaca
The design says "notional marketable limit orders". Alpaca's notional (dollar) orders are market orders; limit orders take a quantity. I compute `qty = notional / limit_price` (4 decimals, fractional) and send a DAY limit order. Untested against the real endpoint; if Alpaca rejects fractional limits for some symbol the order is recorded as rejected and never chased.

### 2.10 Quant score shape
Signals are winsorized (5%) and z-scored within the eligible universe, blended with `strategy.signal_weights`, and squashed with `tanh(z/1.5)` into [−1, 1] so the 20% AI cap is meaningful in absolute terms. Only names above their 50-day MA with a positive ensemble score are candidates; inverse-vol weights; gross = 80% × regime multiplier (1.0 / 0.6 / 0.3). Cash is the remainder.

### 2.11 Backtest is quant-only and survivorship-biased, and says so
Per §11, no historical AI layer is fabricated. The universe is today's list, so the backtest overstates results; use it for *operational* validation (limits hold, no negative cash, exposure ≤ cap, fills at next open) and rough regime behavior, not for return expectations. Rebalance every 5 sessions by default (`--rebalance-days`). Results are reported per calendar year as stability windows; there is no parameter fitting in V1 to walk forward.

### 2.12 Fill model and cron timing: submit after the open, not after the close
The design has decisions after the close and "marketable limit orders during regular market hours". A limit set 15 bps off the *previous close* and queued overnight is not marketable whenever the open gaps up, which is roughly every other day; the first backtest with that literal model realized only 26% average exposure against a 67% target. So the intended flow is: decide on session T's bars, then run the submit step shortly **after the open of T+1** with a fresh reference price (the pipeline already re-checks price drift ≤5%, buying power, position, and market hours immediately before submission). `scripts/run_daily.sh` documents a 9:40 ET cron for paper/live; for shadow/sim the time does not matter. The sim broker models this exactly: fill at T+1's open plus slippage, expire only if the open gapped >5% from the decision reference.

### 2.13 Manual confirmation token format
`SEA_LION_LIVE_CONFIRM_TOKEN` must equal `LIVE-<today's UTC date>`, so it has to be re-set every day, satisfying "manual enablement each day". Plus `SEA_LION_LIVE_ENABLED=1`, ≥30 completed paper sessions in the paper DB, and `--i-understand-live` on the CLI.

## 2b. Real-data results on 2026-09-04

**Quant-only backtest, 2020-01-02 → 2026-08-28, $2,000, 43 names, Yahoo adjusted daily bars, 5 bps slippage, fills at next open.**
Survivorship-biased universe (today's list), so treat as an operational check, not an expectation.

| rebalance | ann. return | ann. vol | Sharpe | max DD | avg exposure | trades | SPY ann. |
|---|---:|---:|---:|---:|---:|---:|---:|
| daily (default; mirrors the pipeline) | 12.5% | 10.6% | 1.17 | −11.1% | 59% | 9,671 | ~13.9% |
| every 5 sessions | 6.4% | 6.8% | 0.94 | −11.7% | 34% | 2,792 | ~13.9% |

Per-year (daily): 2020 +8.1% / 2021 +22.8% / 2022 −9.9% (SPY −19.9%) / 2023 +26.1% / 2024 +15.8% / 2025 +12.6% / 2026 YTD +12.2%.
Reading: the 80% gross cap, 10% per-name cap, and regime scaling do what they are meant to (about half SPY's volatility and a shallower 2022), at the cost of trailing SPY in strong up-years. The 20%/day turnover cap makes weekly rebalancing ramp too slowly, which is why daily is the default. Max drawdown slightly exceeds the 10% safe-mode trigger; in the backtest safe mode is never manually cleared, so the system simply sits until equity recovers to within 10% of the high-water mark, which is faithful to the code.

**First real shadow run (Yahoo bars + headlines, DeepSeek-V4-Flash on the lab GPUs).** 43/43 symbols validated, 12,513 bars, 129 headlines over 2 days. First attempt exposed a real defect: the reasoning model spent the whole 1,200-token budget thinking and returned empty JSON, which the router correctly treated as neutral. Fix: truncation is now detected from `finish_reason`, retried once with a doubled budget, budgets raised (6k/8k), batches cut to 6 events, four batches in flight. After the fix every cheap-tier batch validated (1.5–2k output tokens, 75–105 s each). Results of the completed run are in section 2c.

### 2c. Completed shadow + sim runs, session 2026-09-04

| item | result |
|---|---|
| shadow run `2026-09-04-shadow-a385e241` | status ok, all 9 stages |
| cheap tier | 21 batches, **21/21 valid**, mean 88 s, 21k in / 36k out tokens, $0 |
| main tier | 8 assessments (5 top-quant candidates with news + 3 high-importance events), **8/8 valid**, mean 18 s |
| facts | 55 relevant, deduplicated facts from 129 headlines; 10 with importance ≥ 0.5 |
| AI effect | 4 of 8 assessments cleared the 0.60 confidence floor (MRK 0.70, JNJ 0.65, AMZN 0.65, CVX 0.60); max ensemble contribution +0.05; **top-10 membership unchanged vs quant-only** |
| regime | bull (SPY +1.8% vs MA50, 8.1% realized vol) → 80% gross target |
| proposal | 10 names, 73.9% gross after sector cap (Health Care hit 25%); orders disabled (shadow) |
| sim run `2026-09-04-sim-04db8840` | status ok; 16/21 cheap and 4/8 main calls served from cache (headlines had changed in between); 10 fractional limit buys submitted, $400 total = the 20%/day turnover ramp; fills happen at the next open |
| report | `runtime/reports/{shadow,sim}/latest.html` |

Qualitative check of the theses: the model correctly flagged Apple's CEO transition as a risk rather than a catalyst (impact −0.15, confidence 0.55 → neutral), treated an analyst target raise on a name that is −15% over 20 days as low-confidence, and cited only supplied event ids. No hallucinated evidence ids in 29 calls.

### 2d. Deployment, 2026-09-04 evening

- Alpaca paper keys verified: account ACTIVE, $2,000 equity, no positions; clock, order lookup, bars, latest trades and news all answered.
- **Bars stay on Yahoo**: Alpaca's free IEX feed reported SPY volume of ~1.0M shares on 2026-09-04 versus ~50M+ consolidated, which would fail the $20M dollar-volume screen for most of the universe. **News moved to Alpaca** (Benzinga headlines with exact timestamps).
- `SEA_LION_MODE=paper` in `.env`; `broker.provider: alpaca`, `data.events_provider: alpaca` in the config. Shadow/sim modes still use the local simulator regardless of that setting.
- Decision date rule: before 16:15 ET the run uses the previous session (Yahoo returns the in-progress day as a partial bar); after that, today. Fresh prices at submit time come from Alpaca's latest trade.
- Cron installed for user `sux002` on this server: `40 6 * * 1-5` (9:40 ET) → `scripts/run_daily.sh` → `runtime/logs/cron.log`. Next session is Tuesday 2026-09-08 (Labor Day). The design's shadow gate is subsumed: paper mode carries the same zero capital risk and additionally exercises the broker path, which shadow cannot.
- First paper cycle `2026-09-04-paper-1371f3bd`, run manually at 18:27 ET: status ok; 90 Alpaca headlines ingested; reconciliation clean; **10 fractional DAY limit buys accepted by Alpaca** ($400 total, the 20%/day turnover ramp), confirmed by querying the broker's open orders by `client_order_id`. They queue for the 09-08 open; the 09-09 morning run reconciles the fills.
- That run also exercised a failure path for real: `.env` had no `SEA_LION_LOCAL_LLM_URL`, the router refused the empty host (allowlist), every model call was logged as an error, and the run continued **quant-only** with the `ai_unavailable_quant_only` flag, which paper mode permits by design. Fixed by giving the config `${VAR:-default}` support with the local server as the default and adding the two lines to `.env`, so the 09-08 run will have the AI layer.

## 3. Failure-handling map (design §9)

| Failure | Implemented response | Test |
|---|---|---|
| Market data missing / stale | abort before models and orders; validation report stored | `test_stale_data_aborts_before_models_and_orders` |
| Model timeout / invalid JSON / hijacked output | retry once → neutral; provider marked unavailable; orders blocked outside allowed modes | `test_router.py`, `test_ai_unavailable_*` |
| Broker ambiguous | no retry; query by `client_order_id`; order marked `ambiguous`; safe mode | `test_ambiguous_and_rejected_paths` |
| Partial fill / rejected | fills recomputed from broker; stale remainder cancelled at next reconcile; no chase | `test_sim_broker_fills_and_expires` |
| Local vs broker disagree | safe mode, buys blocked, sells held | `test_reconcile_mismatch_enters_safe_mode` |
| Process crash | `resume <run_id>` loads completed stages; same run id ⇒ same `client_order_id` ⇒ duplicates prevented | `test_resume_after_crash_does_not_duplicate_orders` |

## 4. Acceptance criteria (design §15)

- [x] Replay from stored inputs yields the same pre-trade proposal — `test_replay_reproduces_proposal`, `sea-lion replay`.
- [x] No order bypasses risk validation; every rejection has a machine-readable reason — `risk.py` returns `reasons` per symbol; `execution.py` only accepts `RiskResult.intents`.
- [x] Duplicate-submission tests create at most one broker order — `test_duplicate_submission_creates_one_order`, resume test.
- [x] Stale data, invalid model output, broker ambiguity, state mismatch fail safely — section 3.
- [x] Daily report separates strategy return, benchmark, trading costs, API/data costs — `report.py`.
- [ ] 30 paper sessions before any live order — enforced in `_live_gate`; paper trading started 2026-09-04.

## 5. What is not done / known gaps

- Anthropic provider is written to the SDK docs but not exercised (no key; not needed while the local model does the job).
- The AI tier is slow on DeepSeek-V4-Flash (~10 min per session for ~130 headlines). Qwen3.6 on vLLM with `extra_body: {chat_template_kwargs: {enable_thinking: false}}` should be several times faster if that matters.
- No alerting channel (email/Slack); failures are in `runtime/logs/sea_lion.log`, the run row, and the report. Cron captures stderr.
- Sector tags are hand-maintained in `config/default.yaml`.
- Fees are modeled as $0 (Alpaca) plus 5 bps slippage in sim; real spreads for these names are typically 1–3 bps.
- Forecast calibration by score bucket needs a few weeks of shadow decisions before it is computable; the decision rows already store everything needed.
- The backtest universe has survivorship bias (2.11).

## 6. Operating checklist

1. `source scripts/env.sh && python -m pytest tests -q`
2. `python -m sea_lion.cli check-llm` (a model must be serving; `../llm/status.sh`)
3. Daily: `scripts/run_daily.sh` via cron, `SEA_LION_MODE=sim` for now.
4. Read `runtime/reports/sim/latest.html`; watch for `SAFE MODE` and the reconciliation block.
5. Promote to `paper` after ≥10 clean sessions: add Alpaca paper keys to `.env`, set `broker.provider: alpaca`, `SEA_LION_MODE=paper`, run `check-broker`.
