# Pre-registration: the capitulation entry

Written and committed BEFORE the confirmation run. Nothing below may be edited
after the test executes; a failed test gets recorded as failed.

Everything that produced this rule was measured on SPY and QQQ, 2022-2026.
That data is now burned. The rule is fixed here so the confirmation can be
scored against a bar that was set in advance rather than one chosen to fit
whatever comes back.

## The rule

Long only. One ATM call. No short side, in any market, ever - the
continuation setup lost money in every test that touched it.

**Entry.** On a 15-minute bar, ALL THREE true at that bar's close:

1. `close < open` (a down bar - a capitulation is selling, not a breakout)
2. `volume >= 3.0 x mean(volume of the prior 20 bars)`
3. `RSI(14) <= 30.0`

Fill at the NEXT bar's open. Entry is the flush bar itself - waiting one, two
or three bars after it measured worse every time (+12.6 -> +5.1 -> +4.1).

**Hold.** 8 bars (2 hours). No stop.

**One position at a time.** Signals inside 8 bars of a live one are ignored.

## Deliberately NOT in the rule

Each was measured and each failed. They are listed so the temptation to add
them back after a weak result is on the record as a spec change, not a tweak.

| excluded | why |
|---|---|
| prior-support / volume-shelf confluence | -3.7 bps, and -27 bps in the tightest cut |
| 15m wick-versus-body character | 0.00 weight - contributes nothing |
| stretched to -1.5 sigma or beyond | stacking it dropped +12.6 to -2.3 |
| speed of the approach | backwards alone (-5.3); only a modifier |
| higher-timeframe wick | strong alone, unstable in combination |
| any time-of-day gate | never part of the stated strategy |
| the grind / continuation short | negative with a CI excluding zero |

## Data: what is burned and what is not

**Burned** (used to find the rule, cannot confirm it): SPY and QQQ, 15m,
2022-06 to 2026-07.

**Confirmation set** (untouched by every run so far):

- **IWM**, 15m, 2022-2026 - never scanned, not once, in any script
- **SPY and QQQ, 15m, before 2022-06** - earlier than any window opened

Both are scored. IWM is the cleaner test because the instrument is new as well
as the sample.

## The pass bar

Fixed in advance. All four required:

1. Mean 8-bar forward move **positive**, with a **day-clustered bootstrap 95%
   CI excluding zero**. Clustering by day matters: SPY and QQQ on one day are
   not two draws.
2. Beats the same-period baseline (mean forward move of every down bar in that
   sample) by **at least +5.0 bps**.
3. Hit rate **above 50%**.
4. At least **30 clustered events**, or the sample is declared too thin to
   judge and the result is INCONCLUSIVE rather than a pass.

Anything short of all four is a FAIL. A fail means the rule is not traded and
not tuned into passing - the honest move at that point is to stop, not to
search the confirmation set for a variant that clears the bar, because doing
that burns it too.

## If it passes

Then, and only then, it gets built into Marcus: a `capitulation` signal in the
engine, quoted through the existing execution block, spoken through the
existing copy_trade line. No new voice work is needed - the desk already reads
a code-composed order aloud.

## Prior

Stated up front so a pass is not read as more than it is. The full six-factor
version of this idea went through train/test with a null control and produced
+3.5 bps against a +3.8 bps baseline - no edge at all. This rule is narrower
and its in-sample number was better (+12.6 vs +2.2, n=119), but the base rate
for finding real edge this way is low, and one clean confirmation would not
change that on its own.
