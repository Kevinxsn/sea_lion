# V2 implementation record

What was built against [the V2.1 design](ai_quant_trading_system_v2_design.md), phase by phase, with every deviation and its reason. Written 2026-09-11, the same day as the [week-one review](review_v1.md). All V1 modes and tests still pass; V2 is enabled by default but its trading authority is unchanged: the orders arm is still the V1 headline overlay (arm B) until the promotion gates in §15.5 are met.

## 1. Phase-by-phase status

| Phase (design §21) | Status | Where |
|---|---|---|
| 0 Freeze baseline | Done: `code_version` now records the git hash and a `-dirty` suffix; the false-safe-mode sequence is a regression fixture | `pipeline.code_version`, `tests/test_reconcile.py` |
| 1 Reliability hardening | Done: shared open-status set, reconcile before every early return (`reconcile_only` stage + snapshot + report), exact-quantity exits, audited `sweep-dust`, book-aware position count / sector / dust in every risk record, multiplier preflight (live fails closed) | `store.open_orders`, `pipeline._reconcile_only`, `risk.py`, `execution.sweep_dust`, `broker/alpaca.close_position` |
| 2 Alerts, reporting, measurement | Done: one summary line + exit codes (0 ok / 1 error / 2 attention), sendmail/SMTP notifier with audited delivery attempts, health events, equity + realized-vs-target exposure SVG charts, book-level sector exposure, invocation-date filenames with `run_outcome`, A/B/C divergence (rank, membership, weight distance, orders caused/prevented/resized, notional), hysteresis as a recorded shadow variant | `notify.py`, `report.py`, `arms.py`, `portfolio.select_with_hysteresis` |
| 3 Point-in-time sources + staged schedule | Done: Alpaca trading calendar (early closes) with fallback, SEC EDGAR submissions (acceptance timestamps, 8-K item mapping, primary-doc text) and XBRL fundamentals (point-in-time by `filed`), FRED macro series, `source_documents` with the six timestamps + hashes + revisions + quarantine + source quality + license flags, Yahoo kept as a flagged fallback, after-close `research` job and morning delta path with separate records | `calendar.py`, `data/edgar.py`, `data/fred.py`, `data/documents.py`, `pipeline.research` |
| 4 Canonical event pipeline | Done: entity registry (ticker/CIK/name/aliases), exact + near-duplicate detection, atomic claims with verbatim evidence spans, deterministic cross-run clustering with novelty and supersession, routing policy with reserved filing slots and macro/analyst gates | `events/`, `ai/research.py` |
| 5 Multi-pass research | Done: extractor → (deterministic event builder) → auditor → analyst → skeptic → context → synthesizer, each with strict JSON, per-pass context, cache, retry, validation, deadlines; multi-horizon forecast contract; no thresholds in prompts; abstention rules | `ai/v2_schemas.py`, `ai/v2_prompts.py`, `ai/research.py` |
| 6 Features and portfolio | Done: 15 new registered features (momentum 5/120, trend 20/200, MA slope, residual returns, relative strength vs SPY/sector ETF, downside vol, range, gap risk, beta/corr, drawdown), fundamentals and macro features, correlation-cluster cap, portfolio-beta bound, binary-event concentration, scenario checks; inverse-vol portfolio remains production | `features.py`, `portfolio.py`, `risk.py` |
| 7 Outcomes, calibration, experiments | Done: forecasts frozen at decision time with base prices; 5/10/20-session outcomes scored automatically from adjusted closes vs SPY; Brier/reliability/rank-IC; shrink-to-0.5 until 60 scored samples, then a fitted logistic map per horizon; A/B/C arms with simulated fills from the same cutoff; V2 report sections | `forecasts.py`, `arms.py`, `report.py` |
| 8 Shadow → paper rollout | Started 2026-09-11: arm C runs in shadow beside the paper champion (arm B); promotion is a config change (`v2.arms.orders_arm: C`) after the gates | `config/default.yaml` |

Test suite: 78 tests (was 52), including V2 end-to-end runs with a fake document source and a fake model that answers every pass schema.

## 2. How the daily cycle works now

```
16:30 ET  scripts/run_research.sh  ->  sea-lion research
          bars frozen -> news (Alpaca/Benzinga full text; Yahoo fallback) + SEC filings + FRED + fundamentals (weekly)
          -> validate -> features (+snapshots) -> arm B (V1 headline AI) -> arm C (extract/cluster/route/audit/analyst/skeptic/context/synthesize)
          -> forecasts frozen -> matured outcomes scored -> research_runs watermark

09:40 ET  scripts/run_daily.sh     ->  sea-lion run
          reconcile FIRST (also on holidays / already-decided sessions -> reconcile_only + report)
          -> ingest overnight delta -> reuse research; re-run only candidates touched by new material events (bounded)
          -> arms A/B/C from the same cutoff -> risk on the orders arm (book-aware, clusters, beta, binary events, scenarios)
          -> submit inside the regular-hours decision window only -> shadow arms simulate their own fills
          -> summary line + exit code -> report named by invocation date -> email on attention states
```

The morning path has a hard deadline (`v2.research.morning_deadline_sec`, 10 min): if it is exceeded, buys are held and the report says so.

## 3. Deviations and judgment calls

### 3.1 Event builder (pass B) is deterministic, not a model pass
Clustering uses fingerprints and token/shingle similarity within a time window, with novelty against the symbol's last 30 days and supersession links. It is exact, cacheable, and testable; a model assignment pass can be added behind the same interface if labeled fixtures show the deterministic rule failing. "Complexity must earn its place."

### 3.2 Arm C's overlay reuses the V1 ensemble, with a gate instead of a confidence
The design's fusion formula is implemented in `forecasts.overlay_score` (mean over horizons of 2·(p−0.5), times source-quality × freshness, clamped). To keep the 80/20 blend and the 20% cap shared by all arms, the overlay is passed through the existing `strategy.ensemble` with a binary "usable" flag in place of the V1 confidence: the flag is 1 only when the packet did not abstain, evidence quality clears the floor, and calibration produced a value. No model-reported confidence is compared against a threshold anywhere in V2.

### 3.3 Calibration starts as shrinkage
Until 60 scored outcomes exist per horizon, `p` is shrunk halfway to 0.5 and expected bps are halved. A 1-D logistic recalibration is fitted automatically once the sample exists and is applied thereafter; the uncalibrated history is retained.

### 3.4 Freshness is measured against the decision session, not wall-clock time
So replays, after-close research, and the morning run agree. A five-day-old event contributes nothing.

### 3.5 The morning run trusts the after-close research but still processes deltas
Bars for session T are complete at 16:30 ET, so every feature and forecast the morning decision needs can be computed the evening before. The morning run ingests documents published after the research watermark, extracts and clusters them, and re-runs research only for candidates that received a new actionable event, within the remaining deadline. Without a research snapshot the morning run does everything inline, bounded by the same deadline.

### 3.6 Source quality is heuristic
SEC 0.95, Benzinga 0.70, Yahoo 0.50; listicle-style titles and articles tagged with more than six tickers are discounted to ≤ 0.35; near-duplicates to ≤ 0.40. These are starting values recorded on every document, not tuned parameters.

### 3.7 Alpaca IEX bars still not used
Bars remain Yahoo (Alpaca's free feed is one exchange); the feed identifier is recorded. A consolidated SIP feed is deferred, as the design allows.

### 3.8 Position count counts the resulting book
The engine counts approved targets plus, when a lock keeps existing names, the actual holdings and pending buys; dust below the minimum order size is excluded from the count but listed in every risk record for the sweep. Existing dust (ABBV, $0.28) is closed with `sea-lion --mode paper sweep-dust --confirm`.

### 3.9 Alerts go through the local mail agent
`/usr/sbin/sendmail` exists on the server and port 25 is open; a test delivery to the configured owner address was accepted on 2026-09-11 (delivery attempt #1 recorded as sent). The address lives in `config/default.yaml` under `notify.email_to`, plus `MAILTO` in the crontab. Successful uneventful runs do not email.

### 3.10 Hysteresis runs as a recorded shadow variant, not a fourth arm
Every arm records both the plain top-10 selection and the hysteresis selection with per-symbol reasons; `v2.hysteresis.enabled` switches which one the arm trades. Turning it on for the champion is a config change after the recorded comparison.

## 4. What still needs work

- **Historical event backtest**: not attempted (design §15.2); the AI layers are evaluated prospectively only.
- **ALFRED vintages**: macro uses live FRED observations with `available_at = retrieved_at`; vintages are a research upgrade.
- **Second-provider verification** of high-impact events: interface exists (`ai.main.provider: anthropic`) but is not wired as a separate verifier.
- **Optimizer / covariance**: only cluster caps, a beta bound and deterministic scenarios; no optimizer, by design.
- **Fundamentals coverage**: six XBRL concepts with tag fallbacks; companies using unusual tags show as missing (flagged).
- **Runtime**: the after-close research job is long on the Q4 DeepSeek build (see §5); Qwen3.6 on vLLM with thinking disabled is the documented alternative.

## 5. Real-data verification on 2026-09-11 (shadow mode, DeepSeek-V4-Flash Q4 on the lab GPUs)

Three research runs were needed; each exposed something real.

| run | what happened | fix |
|---|---|---|
| 1 | Sources fine (134 Benzinga articles with full text, 3 near-dups, 5 SEC filings, 1,663 FRED rows, 23,736 XBRL fundamentals rows for 37 CIKs). **0 of 76 extractions valid**: the model omitted the `text` key on every claim because the prompt described fields without naming them. Macro features empty: FRED rows are "available" at retrieval time, after the session's end-of-day cutoff. | Every prompt now ends with an explicit JSON skeleton; schemas accept aliases, truncate over-long strings and coerce unknown enums; macro availability uses the run cutoff; low-quality documents are triaged before extraction and capped at 4 per symbol. |
| 2 | 17 of 17 extractions valid (112 claims, 10 unverifiable spans dropped), 19 events, Oracle's 8-K 2.02 earnings took a reserved filing slot, the tariff story was gated as macro. **All 8 candidates abstained silently**: the daily token budget (400k, sized for cloud pricing) had been consumed by run 1, so audit/analyst passes were skipped without a model call and without a degradation flag. | Local token budget raised to 5M/day; budget exhaustion is now a `degraded` entry, an abstain reason, and marks arm C unavailable. |
| 3 | **Full chain valid**: 77/77 extractions (519 claims, 48 unverifiable spans dropped), 100 canonical events (37 corporate, 26 company news, 20 macro gated, 12 analyst gated unless corroborated, 5 filings), 11/11 audits, 6/6 analyst, 6/6 skeptic, 5/5 context, 5/5 synthesis. 8 forecasts frozen. Runtime 65 min: extraction 33 min (hit the 30-min stage cap, 9 docs left), candidate passes 32 min (hit the cap; JNJ, MSFT, XOM lost to the deadline). | Candidate passes now use all four model slots (was two); the evidence-quality aggregate is claim-weighted instead of a minimum; same-type singular events (earnings, guidance, periodic reports) merge by type so an 8-K and the news about it form one event. |

What the passes decided in run 3 (all frozen, scored automatically from 2026-09-18 onward):

- **ORCL** (real 8-K 2.02 earnings + two news events; audit quality 1.0 on the filing): analyst p≈0.55, skeptic p≈0.45, synthesizer moved to 0.50 with "largely priced in after the +7.5% surge", disagreement 0.3, no abstain, but overlay 0 because the aggregate evidence quality (minimum across the three events) fell under the 0.4 floor. With the claim-weighted aggregate this case would clear the floor and give a small, shrunk positive overlay.
- **CVX, MRK, META, XLE**: news-only evidence audited at 0.3 ("secondary sources, no primary"), skeptic challenges unanswered → abstained with explicit reasons. This is the intended behaviour: Benzinga re-reporting is not primary evidence.
- **JNJ, MSFT, XOM**: lost to the candidate-stage deadline; recorded as `abstain=deadline`, not as opinions.

Takeaways for the next weeks:
1. On this model the after-close job needs ~45–60 min; the morning delta path is what must stay under 10 min. Qwen3.6 on vLLM with thinking disabled is the cheapest speed-up (5–10× per pass) and is a config change.
2. Arm C will be close to quant-only until primary-source events (filings) or multi-source confirmations exist; that is by design, and the A/B/C divergence table will show exactly when it differs.
3. 48 of 567 claims were dropped for unverifiable spans, which is the guard working; none of the 519 kept claims lacked a verbatim source.
