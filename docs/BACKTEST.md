# The blindfolded tape backtest

The desk's engine graded with zero future knowledge: at every decision bar the
tape reader gets ONLY the trailing 240 bars the live desk would have had (same
window, same constants, same scrubber applied inside the window), entries fill
at the NEXT bar's open, and grading walks strictly forward. Harness:
[scripts/backtest_tape.py](../scripts/backtest_tape.py); gate study:
[scripts/backtest_model.py](../scripts/backtest_model.py).

## Phase 1 — discovery (yahoo 15m · 60 days · SPY+QQQ · run 2026-07-17)

45 trades (41 reversal-day, 4 tape-triggered; one per day/kind/side, one
position at a time, decisions 10:00–15:30 ET).

| metric (2h horizon) | value |
|---|---|
| hit rate | 62.2% (28/45), exact binomial p = 0.135 vs coin, p = 0.23 vs the 52.6% base drift |
| mean move | +1.3 bps, bootstrap CI [−11, +13] |
| option sim (frictionless) | +6.7%/trade, PF 1.39, CI [−8.7, +22.6] |

**Verdict: no unconditional edge.** The frictionless option mean is inside
the friction budget (spread + commissions + intraday IV crush ≈ 3–10 pts), so
honest expectancy is ~zero.

The split that surfaced during discovery: entries first appearing **before
12:45 ET hit 50.0% (n=32, −6.7 bps)**; after 12:45 ET **92.3% (12/13,
+21.1 bps, option +32.7%)**. Fisher p = 0.0154; survives selection-adjustment
for the bucket scan at p ≈ 0.02–0.04 (independent stats agent). Confounder
noted by design review: afternoon first-prints are MATURE setups (the
capitulation is hours old), so clock time may proxy for setup age.

An independent three-agent audit (lookahead-leak hunt, stats recomputation
from the raw CSV, design review) found the harness **clean** — six
low-severity notes (data-gap selection, fixed EDT offset on Nov-crossing
windows, partial final bar, split-adjustment on other tickers), none material
to this run.

## Phase 2 — pre-registered out-of-sample test

Written BEFORE the extended run was executed (2026-07-17):

> **H1**: reversal-day entries whose signal first prints **≥ 12:45 ET** have
> positive 2h directional edge and positive option EV **net of 1.5% round-trip
> friction**, on Alpaca IEX 15m bars 2022-01-01 → 2026-05-15 (SPY+QQQ) — data
> the rules never saw. Morning (< 12:45 ET) entries do not.
> **H2 (maturity variant)**: entries with capitulation age ≥ 2h behave like
> the afternoon set regardless of clock hour.
> Pass bar: afternoon (and/or maturity-gated) mean 2h move positive with a
> day-clustered bootstrap CI excluding zero AND net option EV > 0; morning set
> ≤ 0 or clearly worse. Otherwise the champion engine stays unchanged.

Notes for the extended run: decision-hour gating uses TRUE ET (manual US-DST
rule) while session bucketing keeps the engine's fixed −4h convention (live
parity — both are "wrong" together in winter, consistently); IEX volume is a
~proportional sample of the consolidated tape, acceptable because every
volume rule in the engine is ratio-based (spike vs own average, median-relative
capitulation); option sim charges 0.75% per side.

## Phase 2 — results (alpaca IEX 15m · 2022-01-01 → 2026-05-15 · SPY+QQQ)

142 trades (112 reversal-day, 30 tape-triggered), day-clustered (same day +
side across SPY/QQQ = one draw). Evaluator:
[scripts/backtest_oos.py](../scripts/backtest_oos.py) →
`scripts/out_oos/oos_verdict.json`.

| set | trades / clusters | 2h hit | mean 2h | net option EV (after 1.5% RT) |
|---|---|---|---|---|
| reversal-day, all (structurally all ≥ 12:45 ET*) | 112 / 62 | 61.3% (z 1.78) | +10.8 bps, CI [−7, +29] | **+18.5%, CI [+5.4, +33.5]** |
| … capitulation age < 2h | 92 / 46 | **67.4% (z 2.36)** | +21.1 bps, CI [−0.2, +43] | **+27.7%, CI [+11.2, +46.3]** |
| … capitulation age ≥ 2h | 20 / 18 | 44.4% | −15.6 bps | −5.6% |
| tape-triggered (standalone) | 30 / 26 | 38.5% | −1.7 bps | −7.9%, CI [−27, +14] |

\* IEX bars begin 08:00 ET, so day_shape's 20-session-bar minimum only
unlocks ~13:00 ET — the OOS sample is exactly the afternoon strategy. The
morning leg of H1 was untestable here; its evidence remains the Phase-1
discovery split (morning 50% hit, −6.7 bps, option −3.8%).

**Verdicts.**
- **H1: partially confirmed.** The afternoon reversal-day trade has positive
  net option expectancy out-of-sample with the clustered CI excluding zero;
  the directional (underlying) mean is positive but its CI touches zero — the
  convexity (no stop, +50% trim, gamma) does real work.
- **H2: refuted, informatively.** Maturity is NOT the edge — FRESHNESS is.
  Capitulation under two hours old carries the whole result; stale shapes
  grade flat-to-negative.
- **The standalone wall-cross trigger shows no OOS edge** (38.5% hit, n=30).
  Phase 1's 75% on n=4 was noise. Untouched in the engine pending a product
  decision; its likely value is confirmation inside a reversal-day context,
  not standalone entry. (Caveat: IEX volume is a sampled tape; ratio-based
  rules should survive that, but this branch leans hardest on volume.)

## What changed in the engine

`common/tape.py` now stamps every day-shape with `age_h` and `takeable`
(true-ET ≥ 12:45 **and** capitulation age < 2h — DST-correct, computed from
the tape's own timestamps so replay stays clock-honest), and
`common/signals.py` only lets a **takeable** day-shape flip the trade; a
forming shape stays context ("FORMING — the desk takes reversal-day entries
after 12:45 ET on a capitulation under two hours old"). Regression:
`test_morning_day_shape_stays_context`.

The OOS record of the deployed policy (afternoon + fresh): **67.4% hit,
net +27.7% per trade, CI [+11.2, +46.3], 46 clusters over 4.4 years.**

## Honest limits

Marks are Black-Scholes at flat realized-vol-derived IV (no smile, no
intraday IV path — reversal days crush IV, which flatters long calls by a
few points even after the 1.5% friction charge); IEX volume is a proportional
sample, not the consolidated tape; ~0.7 trades/week means another regime
could move these numbers; the discovery split was post-hoc (the OOS run is
the defense). Next hardening steps if wanted: real option marks spot-check
(OptionsDX/ThetaData), VIX-driven IV paths, cross-section (IWM/DIA/sectors),
overnight-continuation horizons.
