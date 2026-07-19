# The reasoning layer

A plan. Nothing here is built yet.

## Where we actually are

The desk is a deterministic rules engine plus a speech model that narrates it.
There is no reasoning model anywhere in the stack: `gpt-realtime-2.1` picks the
words, `gpt-4o-mini-transcribe` hears the caller, and `common/signals.py`
decides the trade with hand-written rules. `agents/05_desk_analyst/main.py`,
the one component that ever reasoned, was deleted in `86d3f54`.

What we do have is rarer and worth more: **a blindfolded backtest with a
pre-registered out-of-sample result.**

| bucket (OOS, after 1.5% round-trip) | n | hit | net option EV |
|---|---|---|---|
| reversal-day, capitulation age < 2h | 112 / 62 clusters | **67.4%** (z 2.36) | **+27.7%**, CI [+11.2, +46.3] |
| reversal-day, capitulation age >= 2h | 20 / 18 | 44.4% | **-5.6%** |
| tape-triggered standalone (wall cross) | 30 / 26 | 38.5% | **-7.9%** |

Three facts follow, and they set the whole design:

1. **There is a real edge**, cost-adjusted, out-of-sample, CI excluding zero.
2. **There are measured losers** in the same signal set.
3. **`recommend_trade(ticker, as_of=...)` already implements the blindfold**, so
   anything new can be graded through the identical door with no new harness.

## The bar

Any reasoning layer must beat **+27.7% net EV** on the same harness, on
out-of-sample data, after costs. Not "sounds smarter". Not "explains better".
If it does not beat the number, it does not ship. The harness is what makes
this an engineering project instead of a story.

## The first win is subtraction, not addition

The cheapest available EV is **refusing the buckets that already lose money**.
Age >= 2h is -5.6%. Standalone wall-cross is -7.9%. A layer that declines those
raises portfolio EV without discovering anything at all, and it is far more
reliable than finding new alpha. **Abstention is a first-class output**, and the
first thing we measure.

Only after the filter is proven do we let the model propose trades the rules
engine would not have taken.

## Model

**Claude Opus 4.8** (`claude-opus-4-8`), $5 / $25 per MTok, 1M context.

- Adaptive thinking with an effort dial (`low` -> `max`). Backtests run at
  `medium`; live decisions at `high`/`xhigh`.
- Prompt caching cuts the cached prefix to ~0.1x. Our system prompt, the house
  rules and the strategy definitions are byte-identical across every decision,
  so nearly the whole prompt caches.
- Batch API is 50% off and backtests are inherently offline.

Rejected: **Claude Fable 5** ($10/$50) is stronger but 2x the price with
always-on thinking, and backtest volume is the cost driver. Revisit if Opus 4.8
plateaus. **grok-4.5** is already wired but is the weakest of the three at
multi-step reasoning, which is the exact thing being bought. **Sonnet 5**
($3/$15) is the fallback if cost becomes binding.

Needs `ANTHROPIC_API_KEY` in SSM Parameter Store alongside the existing keys.

### Backtest economics

The reasoner is invoked **only at candidate moments** the deterministic engine
already flags (armed / confirming / triggered), not on every bar. The OOS study
had 112 trades across 62 clusters, so a full replay is hundreds to low
thousands of calls, not tens of thousands.

Per call: ~20k input (of which ~15k caches), ~3k output.

| | per call | 2,000 calls |
|---|---|---|
| naive | $0.175 | $350 |
| + prompt caching | $0.108 | $216 |
| + Batch API | **$0.054** | **$108** |

A full 4.4-year replay costs about a hundred dollars. That is cheap enough to
run on every prompt change, which is the entire point.

## Architecture

LangGraph, because the loop shape is the deliverable as much as the P&L.

```
   candidate moment (as_of)
            |
      [ gather ]      point-in-time tools ONLY; the blindfold is enforced
            |         in the tool layer, not by asking the model to behave
      [ analyze ]     adaptive thinking, effort=high
            |
      [ decide ]      structured output: TRADE | ABSTAIN, with reasons
            |
      [ refute  ]     adversarial pass: argue the decision is wrong
            |         (this is where the known-loser buckets get caught)
      [ commit  ]     emit, or abstain and say which rule killed it
```

**Tools** wrap what the engine already computes, each taking `as_of`:

- `gex_snapshot` - regime, flip, walls, net GEX
- `tape_read` - bands (u2/u1/vwap/d1/d2), stage, day shape, capitulation age
- `confluence_board` - the five boxes and why each is green/gray
- `chain_quote` - Black-Scholes marks for a strike
- `history_lookup` - what happened in comparable past setups

The blindfold lives in the tools. A tool that can see the future is a bug the
model cannot detect, so `as_of` is enforced at the data layer and unit-tested
the way `backtest_tape.py` already is.

**Structured output** (`output_config.format`) so a decision is a validated
object, never prose. The voice layer then reads fields, exactly as it now reads
`copy_trade`.

## Phases, each with a gate

**1. Harness first.** Wire the LangGraph loop into `backtest_tape.py` as an
alternative decision function. Run it with a stub that always mirrors the rules
engine. Gate: the stub reproduces the existing OOS numbers exactly. If it does
not, the integration is wrong and nothing downstream means anything.

**2. The filter.** Reasoner may only ABSTAIN or PASS THROUGH; it cannot alter a
trade. Gate: net EV on the OOS set improves versus +27.7%, driven by declining
the -5.6% and -7.9% buckets. This is the cheapest possible win and it validates
the whole pipeline.

**3. Proposal.** Reasoner may now adjust strike, size, and entry condition.
Gate: beats phase 2 out-of-sample, not in-sample.

**4. Live.** Same loop behind `trade_recommendation`, effort=high. The voice
layer changes almost not at all: it already reads a `copy_trade` string built
by code. Now that string comes from the loop.

**5. Voice last.** Per the brief, the voice only needs to not be weird.

## What would make this fail

- **Overfitting to the OOS set.** It stops being out-of-sample the moment we
  tune against it. Hold back a final slice, touched once.
- **Lookahead through a tool.** The single most dangerous bug class, and it
  produces beautiful backtests. Every tool needs a leak test.
- **Grading on prose.** If we ever judge this by how good the reasoning sounds
  rather than by net EV, the project is over and we will not notice.
- **n is small.** 112 trades is a real result, not a large one. Confidence
  intervals stay in every report.
