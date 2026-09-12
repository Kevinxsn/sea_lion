# Sea Lion

**An AI-assisted, risk-gated, low-frequency, long-only U.S. equities system for a $2,000 experimental account.**
V1 goal: run the complete daily research → proposal → risk → (paper) execution → reconciliation → report loop reliably in simulation, with every decision reproducible from stored inputs. Profit is not the V1 success criterion; operational reliability and traceability are.

Design document: [docs/ai_quant_trading_system_v1_design.md](docs/ai_quant_trading_system_v1_design.md).
What was built and where it deviates from the design: [docs/v1_implementation.md](docs/v1_implementation.md).

> Not investment advice. Research software. It can lose everything deployed; paper results may not reproduce live.

## Where things run

Everything runs on the lab server (`mediator`). The AI layer is **DeepSeek-V4-Flash served locally** by llama.cpp on GPUs 0–3 (port 8324, from `../llm/`), so no data leaves the machine and model calls cost $0; Qwen3.6 on vLLM (port 8321) is a drop-in alternative. Market bars come from Yahoo Finance (free), headlines from Alpaca's news API, and orders go to an **Alpaca paper account** ($2,000 virtual). A cron job on this server runs the cycle every weekday at 9:40 ET.

## The idea in one paragraph

Once per trading day after the close, the system pulls daily bars for ~45 liquid large caps and ETFs, validates them, computes a handful of explainable quantitative signals (momentum, trend, volatility, liquidity, market regime), and uses a local LLM to turn recent headlines into structured, bounded "event facts" and a short thesis for the top few candidates. The AI can move a name's score by at most 20%. A deterministic risk engine then clamps everything (80% max gross, 10% per name, 25% per sector, 10 names, 2% daily-loss lock, 10% drawdown safe mode, 20% daily turnover, order-size bounds) and emits order intents. Orders are marketable limit orders for the next session with deterministic IDs so nothing can be submitted twice. The next run reconciles fills against the broker; any disagreement puts the system into safe mode, which blocks new exposure until a human clears it.

## Modes

| mode | data | AI | broker | orders | use |
|---|---|---|---|---|---|
| `backtest` | Yahoo history | off (by design) | in-memory simulator | simulated | multi-year quant-only evaluation |
| `shadow` | live Yahoo | on | none | never | "would have done" decisions, zero risk |
| `sim` | live Yahoo | on | **local fill simulator**, no keys | simulated next-open fills | full loop without a broker account |
| `paper` | Yahoo bars + Alpaca news | on | Alpaca paper | real paper orders | **current phase**; 30-session gate before live |
| `live` | Alpaca | on | Alpaca live | real money | needs env flag + daily token + 30 paper sessions + `--i-understand-live` |

## Quick start

```bash
cd /projects/ps-renlab2/sux002/sea_lion
source scripts/env.sh                  # NVMe venv on PATH, loads .env
cp .env.example .env                   # fill in keys when you have them (optional for shadow/sim)

sea-lion check-llm                     # local LLM reachable + returns valid JSON
sea-lion --mode shadow research        # after-close research job (documents, features, arm B + arm C)
sea-lion --mode shadow run             # morning decision cycle, no orders
sea-lion --mode sim run                # one cycle with simulated fills
sea-lion --mode sim status             # equity, safe mode, open orders, budget
sea-lion --mode backtest backtest --start 2020-01-02
python -m pytest tests -q              # 46 tests, ~1 min
```

Reports land in `runtime/reports/<mode>/<date>_<run_id>.html` (and `latest.html`).
`runtime/` is a symlink to `/data/tmp/sux002-sea-lion` (local NVMe: venv, SQLite DBs, snapshots, logs, reports). Nothing there is committed; nothing in git holds secrets.

Daily automation: `scripts/run_daily.sh` from cron on weekdays after the close (example line in the script).

## Repository map

```
config/default.yaml        all policy: universe+sectors, signals, limits, AI routing, budgets  (git-versioned, hashed into every run)
sea_lion/
  config.py                typed settings; ${ENV} substitution; paper/live DB separation; v2 + notify sections
  calendar.py              official sessions/early closes (Alpaca) with fallback; decision window; last completed session
  notify.py                health events + email (sendmail/SMTP/fake) with audited delivery attempts
  data/documents.py        point-in-time SourceDocument, hashing, dedup, quarantine, on-disk bodies
  data/edgar.py, fred.py   SEC EDGAR filings + XBRL fundamentals; FRED macro
  events/                  entity registry, canonical-event clustering, routing policy
  ai/v2_*.py, ai/research.py   V2 pass contracts, prompts, and the multi-pass orchestrator with deadlines
  portfolio.py             rank hysteresis, correlation clusters, beta, scenario checks
  forecasts.py             frozen forecasts, 5/10/20-session outcomes, calibration, overlay
  arms.py                  A/B/C champion/challenger portfolios, shadow fills, divergence
  store.py                 SQLite: runs, stage artifacts, bars, events, AI cache, model calls, decisions, orders, equity, costs, safe mode
  data/                    market data + events: yfinance (default, free), alpaca, file, validation
  features.py              point-in-time rolling features; cross-sectional winsorize+z-score; regime
  ai/                      schemas (pydantic contracts), prompts (versioned), providers (local OpenAI-compatible / Anthropic), router (cache, budget, validation, retry-once-then-neutral)
  strategy.py              quant score, capped AI ensemble, top-N selection, inverse-vol weights
  risk.py                  pure deterministic risk engine + pre-submit checks
  broker/                  base protocol, sim (local fills), alpaca (paper/live)
  execution.py             deterministic client_order_id, idempotent submit, kill switch
  reconcile.py             order status sync, stale-order cancel, position mismatch → safe mode
  pipeline.py              stage orchestrator: ingest→validate→account→features→ai→propose→risk→execute→report; resume; replay
  backtest.py              walk-forward quant-only backtest using the same components
  report.py                metrics + JSON/HTML daily report
  cli.py                   run / resume / replay / backtest / status / safe-mode / kill / check-llm / check-broker
tests/                     unit + end-to-end tests (fake market data, fake LLM, sim broker)
scripts/                   env.sh, run_daily.sh (cron entry)
docs/                      design doc, implementation record
```

## How a run is traced

Every run has a `run_id`; every stage writes an artifact row before the next stage starts. The ingest stage freezes the exact bar snapshot to parquet and records its hash; model outputs are cached by (tier, model, prompt version, schema version, input hash). `sea-lion replay <run_id>` rebuilds the proposal from those stored inputs with **no network and no model calls** and reports whether it matches. `sea-lion resume <run_id>` continues a crashed run; because `client_order_id` embeds the run id, a resumed execute stage cannot double-submit.

## Safety controls you should know

- **Kill switch:** `sea-lion --mode <mode> kill` cancels all open orders and enters safe mode. Nothing is ever auto-liquidated.
- **Safe mode:** entered on drawdown ≥10%, reconciliation mismatch, or ambiguous broker response. Buys are blocked; risk-reducing sells are *listed for manual review*, not submitted. Clear with `sea-lion safe-mode --clear --who <you>` after review (`accept-broker-state` if the broker is right and we were wrong).
- **Live gate:** `SEA_LION_LIVE_ENABLED=1`, `SEA_LION_LIVE_CONFIRM_TOKEN=LIVE-<today>` (must be re-set every day), ≥30 completed paper sessions, and `--i-understand-live`. Paper and live use separate keys and separate databases.
- **AI can never**: pick order types, sizes, accounts, or credentials. It returns validated JSON; prose is stored, never parsed into orders. Headlines are wrapped as untrusted data. Invalid output → retry once → neutral.
- **Budgets:** per-day and per-month USD/token caps; main-model calls stop at 80%, everything stops at 100%. The local model costs $0 but is still metered.

## V2 (2026-09-11)

V2 upgrades the research layer without changing who may trade: **evidence-driven, point-in-time, measured**. Design: [docs/ai_quant_trading_system_v2_design.md](docs/ai_quant_trading_system_v2_design.md); what was built and why: [docs/v2_implementation.md](docs/v2_implementation.md).

- **Two jobs a day.** `sea-lion research` after the close (documents, features, both AI arms) and `sea-lion run` in the morning (reconcile first, overnight delta, decision, orders in regular hours only). Both print one `SEA_LION_SUMMARY` line; exit code 2 means attention.
- **Sources with timestamps.** Alpaca/Benzinga full-text news, SEC EDGAR filings (8-K/10-Q/10-K with acceptance times) and XBRL fundamentals, FRED macro, Yahoo as a flagged fallback. Every document has published/retrieved/available times, a hash, revisions, quality, and a quarantine path.
- **Canonical events, not headlines.** Claims with verbatim evidence spans → clustered events with novelty and supersession → routing (filings first; macro and analyst notes gated) → audit → analyst → skeptic → context → synthesis into a 5/10/20-day forecast that can abstain.
- **Three arms every day** from the same cutoff: A quant-only, B V1 headline overlay (current champion, trades), C V2 verified-event overlay (shadow, simulated fills). The report says whether the AI changed rank, membership, weights, or orders.
- **Forecasts are frozen and scored** at 5/10/20 sessions; probabilities are shrunk toward 0.5 until 60 scored outcomes exist, then recalibrated.
- **Risk sees the real book**: position count, sector, dust, pending orders, correlation clusters, portfolio beta, binary-event concentration, scenario exposures. Alerts by email on safe mode, aborts, AI fallback, ambiguous orders, missed deadlines.

## Week-one review (2026-09-11)

Read [docs/review_v1.md](docs/review_v1.md). Short version: the loop ran every weekday, 19 of 21 orders filled, no duplicates or crashes; a bookkeeping bug (Alpaca's `pending_new` status missing from our open-order query) put the system into safe mode on Sep 10, which is exactly the fail-safe behaviour intended, and is fixed with regression tests. Safe mode must be cleared by a person: `sea-lion --mode paper safe-mode --clear --who <you>`. Add `MAILTO` to the crontab so this cannot go unnoticed again.

## Current status (2026-09-04)

- **Phase: paper trading on Alpaca, started 2026-09-04.** The first cycle placed 10 fractional limit buys ($400 total, the 20%/day turnover ramp) that Alpaca accepted; they fill at the next open, Tuesday 2026-09-08 (Labor Day Monday). From then on the cron job runs every weekday at 9:40 ET and each morning run reconciles the previous day's fills before deciding.
- Validated so far: 48 tests; real-data shadow and sim runs with the local LLM (29/29 model calls valid); a 2020–2026 quant-only backtest (~12.5%/yr, 10.6% vol, Sharpe 1.2, max DD 11%, survivorship-biased); Alpaca account/clock/data/news/order/reconcile paths against the live paper API. Details and caveats: [docs/v1_implementation.md](docs/v1_implementation.md) §2b–2d.
- Gate to a live pilot: 30 clean paper sessions plus an operational review. Nothing can trade real money before that.

## Daily operations

```bash
source scripts/env.sh
sea-lion --mode paper status               # equity, safe mode, open orders, sessions completed, AI budget
open runtime/reports/paper/latest.html     # today's decisions, risk reasons, orders, costs
tail -f runtime/logs/cron.log              # what cron did this morning
sea-lion --mode paper safe-mode --clear --who <you>   # only after reviewing a SAFE MODE banner
sea-lion --mode paper kill                 # emergency: cancel all open orders, block new exposure
sea-lion --mode paper run --dry-run        # full morning path without submitting
sea-lion --mode paper sweep-dust [--confirm]   # list / close positions below the minimum order size
sea-lion --mode paper score-outcomes       # score matured forecasts, print calibration
sea-lion --mode paper notify-test          # send a test alert through the configured transport
sea-lion --mode paper calendar             # session / decision-window state
```

If the LLM server is down at 9:40 ET the run proceeds quant-only and says so in the report; bring it back with `../llm/serve.sh v4gguf --daemon`.
