# V1 review after the first week of paper trading

Period reviewed: 2026-09-04 (deployment) → 2026-09-11 (this review). Sources: the paper database, `runtime/logs`, the daily HTML reports, and the live Alpaca paper account. Written 2026-09-11.

## 1. Verdict in three lines

- **Operationally the system did what it was designed to do**, including failing safe: 5 cron firings, 3 completed decision cycles, 21 orders at Alpaca (19 filled, 2 expired), 29 of 29 model calls valid on the days the AI ran, no crash, no duplicate order, no ambiguous broker response.
- **It has been sitting in safe mode since Sep 10 because of a one-line bug in our own bookkeeping**, not because of anything the broker did. Nobody noticed for two days because there is no alerting. Those are the two things to fix immediately.
- **Performance is noise at this sample size** and the AI layer has not yet changed a single decision. That is expected and is documented below so we know what to measure next.

## 2. What happened, day by day

| date (ET) | cron | as_of used | outcome |
|---|---|---|---|
| Fri 09-04 18:27 (manual) | — | 09-04 | First cycle. AI failed on a missing env var → quant-only fallback (allowed in paper). 10 buys queued for the next open. |
| Mon 09-07 09:40 | fired | 09-04 → skipped | Labor Day. Correctly skipped (session already decided). |
| Tue 09-08 09:23–09:42 | — | — | 8 of 10 overnight orders filled at the open, 1.3% below their limits (gap down); XLE and CVX expired (energy gapped up). |
| Tue 09-08 09:40 | fired | 09-04 → skipped | Correctly skipped, **but skipped runs did not reconcile**, so Tuesday's fills were only recorded Wednesday. |
| Wed 09-09 09:40 | ok, 9.3 min | 09-08 | Reconciled the 8 fills. AI ran fully (16 + 8 valid calls). 11 orders submitted 09:49, **all filled within seconds** ~5 bps inside the limit. Sold ABBV, added XOM. |
| Thu 09-10 09:40 | ok, 10.8 min | 09-09 | Reconciliation flagged all 11 positions as mismatched → **safe mode**. No orders. |
| Fri 09-11 09:40 | ok, 10.2 min | 09-10 | Still safe mode. No orders. Regime flipped to neutral (SPY −1.6% on the week). |

Account on 09-11: equity $1,994.11 (−0.29%), cash $1,388.80, 11 positions worth $605 (30% exposure; target would be ~76% in bull, ~48% now in neutral). SPY: −1.6% over the same days. The strategy was 30% invested, so its relative "outperformance" is exposure, not skill.

## 3. Problems to fix now (ordered by severity)

### P1. False safe mode from an incomplete open-order status list — **fixed in this review, tests added**
`Store.open_orders()` listed `new, submitted, partially_filled, accepted`. Alpaca answers a fresh submission with `pending_new`. Every order from the 09-09 run was stored with that status, so the next reconciliation never refreshed them, never added their fills to `expected_positions`, and compared a stale expectation against the broker: 11 "mismatches", safe mode. The broker was right the whole time.
Fix: the store now derives the set from `broker.base.OPEN_STATUSES` (single source of truth). Regression tests: `tests/test_reconcile.py`.
**Action for you:** the code fix does not clear safe mode by design (a human must). After confirming the reconciliation is clean, run
`sea-lion --mode paper safe-mode --clear --who <you>` before Monday 09:40 ET, otherwise Monday's run will also do nothing.

### P2. No alerting — a blocked system looked exactly like a healthy one
Safe mode, aborted runs, and quant-only fallbacks are all visible in the report, and nowhere else. Two trading days passed. Minimum viable fix (next change): cron `MAILTO`, plus `run_daily.sh` printing a one-line summary (`status`, `safe_mode`, `n_orders`, `ai_available`) and exiting non-zero on safe mode so cron mails it. Better: a small `notify` hook (email or Slack webhook) called from the pipeline on `enter_safe_mode`, `aborted_*`, `error`, `ai_unavailable`.

### P3. Skipped-session runs skipped reconciliation — **fixed in this review**
On holidays and on any morning when the previous session was already decided, `run()` returned before touching the broker. Fills from that morning's open were not recorded until the following day, which also delays detecting real mismatches by a day. Now a skipped run still reconciles (stage `reconcile_only`).

### P4. Dust positions from quantity rounding — **fixed in this review, test added**
The 09-08 rebalance meant to exit ABBV entirely but sold 0.1809 of 0.182 shares, leaving $0.28 that can never be sold by a notional order ($1 minimum) and that counts as an 11th position. Cause: sell quantity was derived from `notional / limit_price` with a fresh price higher than the close. Now, when an exit would leave less than the $10 minimum, the intent is flagged `close_position` and the exact held quantity is sent. The existing ABBV dust needs a one-off cleanup (Alpaca's close-position endpoint; a `sea-lion sweep-dust` command is a small follow-up).

### P5. Overnight orders expire on gaps (confirmed live)
2 of 10 overnight orders expired; 11 of 11 morning orders filled in seconds. This confirms the fill-model decision from the implementation doc. Nothing to change, but **never run the paper/live cycle after the close again**; the cron time is right.

### P6. The paper account moved to `multiplier: 1`
Alpaca reported `multiplier 4` on 09-04 and `1` on 09-11 — someone (probably the account settings) switched the paper account to cash-only. That is exactly what the design wants (no margin). Keep it. Worth a check in `check-broker` that fails if multiplier > 1 in live mode.

## 4. What is working and should stay

- **Fail-safe chain**: env var missing → quant-only with a flag; reconciliation doubt → safe mode with buys blocked and holdings untouched. Both fired for real this week and neither lost money or duplicated an order.
- **Execution quality**: morning marketable limits at +15 bps filled at about +3 to +5 bps against the reference trade; overnight fills landed 1.3% *better* than limit on a gap down. Slippage assumption of 5 bps in the sim is, if anything, generous.
- **Idempotency**: 21 orders, 21 deterministic ids, zero duplicates, including across the crash-free but skipped days.
- **Model contract**: after the token-budget fix, 0 invalid outputs in 75 real calls; no invented evidence ids; injection-style headlines did not alter outputs.
- **Data**: 43/43 symbols validated every day; the decision date rule (previous session before 16:15 ET) worked across a holiday weekend.

## 5. Things to improve (not urgent, in suggested order)

1. **Rank-boundary churn.** Each day one name dropped out of the top 10 and one came in (ABBV→XOM, XLV→META, V→AAPL). Every swap is a round trip at ~10 bps all-in, on a signal whose edge per day is far smaller. Add hysteresis: keep a holding while it is still in the top 15 (or its score is within a band of the 10th), only replace when it falls out of that band. Expect turnover to drop by half or more.
2. **AI confidence is anchored at the floor.** 13 of 24 main-tier assessments reported confidence of exactly 0.60, the number the prompt reveals as the threshold; only 1 of 24 was below 0.55. The floor is therefore not discriminating. Options: stop revealing the threshold in the prompt; ask for a probability that the sign of the move is right and calibrate it against realized 5/10/20-day returns once ~60 assessments exist; or raise the floor to 0.65 until then. The decision table already stores everything needed for the calibration.
3. **AI has not changed a decision in 4 cycles.** Max contribution ±0.06 on a score scale of [−1, 1]; membership identical to quant-only every day. That is the 20% cap working as intended, but it also means the paper phase is not yet producing evidence about the AI layer. Log a per-run "would the AI-assisted and quant-only portfolios differ, and by how much" metric so the 30-session review can say something.
4. **Headline mix is mostly macro noise.** 43 of 144 facts were `macro`, 25 `analyst`; only 5 were earnings/guidance. Alpaca/Benzinga headlines are shallow. Cheap wins: drop `macro` facts from the main-tier context unless importance ≥ 0.7; deduplicate across days (same story re-syndicated); consider SEC 8-K feeds (free) as the only "approved events" for the main tier.
5. **The AI stage is 95% of the 10-minute runtime.** 6-event batches at ~90 s each on the Q4 DeepSeek-V4 GGUF. Qwen3.6-27B on vLLM with thinking disabled should cut this to ~1 minute; or run the cheap tier at 16:30 ET the day before (headlines are known then) and only the main tier in the morning. Cost is $0 either way; the point is a tighter window between the fresh price and the order.
6. **Position count and sector caps use the proposal, not the book.** With dust and lagging exits the book had 11 names. Make the risk engine count actual holdings plus new entries, and make the report show book-level sector exposure.
7. **Reports for skipped runs** are written under the previous session's date (`2026-09-04_2026-09-06-…`). Cosmetic, but confusing when scanning the folder; name them by run date and mark them skipped.
8. **Turnover cap semantics on day one.** The 20%/day cap makes a fresh account take about four sessions to reach target exposure. Fine for a $2k experiment; for a larger account consider a separate "initial deployment" allowance.
9. **Backtest realism.** The sim's next-open fill now matches live behaviour well; add the observed +4 bps morning slippage and the ~20% chance of an overnight expiry as parameters so the backtest can be run under both submission styles.
10. **Performance reporting.** The daily report shows "days: 3" and a small-sample warning, which is correct. Add a plain equity-curve chart and the realized exposure series so the 30-session review is a picture, not a table.

## 6. Metrics for the 30-session gate (design §11)

Track these from the paper DB; today's values in brackets: completed sessions [3], reconciliation failures excluding the P1 bug [0], duplicate orders [0], invalid model outputs [0 / 75], aborted runs other than holidays [0], safe-mode entries [1, false positive], average exposure vs target [30% vs 76%], turnover per session [~20% of equity], fills within limit [19/21], AI-vs-quant portfolio difference [0.0 every day].

## 7. Immediate checklist

1. Commit the fixes in this review (`store.py`, `risk.py`, `execution.py`, `pipeline.py`, `tests/test_reconcile.py`, this file).
2. Confirm reconciliation is clean with the fixed code: `sea-lion --mode paper status` should show no open orders and no mismatch in the latest reconciliation.
3. Clear safe mode: `sea-lion --mode paper safe-mode --clear --who <you>`.
4. Add `MAILTO=<your email>` to the crontab so failures reach you (P2).
5. Optional: close the ABBV dust position from the Alpaca dashboard.
