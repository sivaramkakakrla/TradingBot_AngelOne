---
name: candlestick-pattern-playbook
description: 'Scan and trade candlestick patterns with context, confirmation, and risk controls. Use when analyzing hammer, engulfing, doji, star, pin bar, inside bar, and continuation setups for intraday or swing decisions.'
argument-hint: '[symbol] [timeframe] [optional: bullish|bearish|neutral]'
---

# Candlestick Pattern Playbook

Use this skill to turn candlestick observations into a repeatable decision workflow.

## What This Skill Produces
- A structured read of market context before pattern selection.
- A pattern classification (reversal, continuation, indecision).
- A confirmation-based entry plan (not candle-only guessing).
- A risk plan with stop, target, and position sizing checks.
- A no-trade outcome when quality criteria are not met.

## When To Use
- You spot one or more candles and want an objective trade/no-trade decision.
- You want to avoid overfitting by memorizing too many pattern names.
- You need consistent rules across equities, indices, options, forex, or crypto charts.

## Procedure
1. Define chart context first.
- Mark trend state on the execution timeframe: uptrend, downtrend, or range.
- Mark location: major support/resistance, prior swing high/low, VWAP/EMA zone, gap edge, or trendline.
- Check higher timeframe alignment (at least one level above execution timeframe).

2. Score candle quality.
- Evaluate size: is momentum expanding or contracting?
- Evaluate wick behavior: clear rejection or random noise?
- Evaluate body close location: strong close near candle extreme or weak mid-range close?
- Tag the pattern family:
  - Reversal examples: hammer, shooting star, engulfing, morning/evening star.
  - Continuation examples: rising/falling three methods, marubozu, inside-bar continuation.
  - Indecision examples: doji, spinning top, long-legged doji.

3. Apply confirmation logic.
- Wait for confirmation candle or breakout trigger, especially for doji/indecision setups.
- Require volume confirmation where available (relative expansion vs recent average).
- Prefer confirmation that breaks pattern high/low in the expected direction.

4. Build trade plan.
- Entry:
  - Breakout entry above/below trigger candle, or
  - Retest entry only if structure remains valid.
- Stop:
  - Beyond invalidation level (pattern extreme or structural level), not arbitrary points.
- Target:
  - First target at nearest opposing structure.
  - Secondary target by risk multiple.
- Minimum quality rule: target should offer at least 1.5R unless strategy specifies otherwise.

5. Size risk before order placement.
- Set max risk per trade (fixed amount or fixed percent of capital).
- Position size formula:
  - position_size = max_risk / abs(entry - stop)
- Skip trade if slippage/liquidity makes effective risk exceed plan.

6. Manage after entry.
- If confirmation fails quickly, respect stop without averaging down.
- At first target, reduce risk (partial exit or stop-to-breakeven based on strategy).
- For trend continuation, trail below/above structure instead of fixed-point exits.

7. Journal and review.
- Log setup type, location quality, confirmation quality, risk multiple outcome, and screenshot.
- Review weekly for false-signal clusters (for example, low-volume ranges).

## Decision Branches
- If trend context is unclear and location is weak: no trade.
- If pattern is indecision and no confirmation appears: wait.
- If setup is counter-trend:
  - Require stronger confluence (major level + strong rejection + confirmation).
  - Reduce size or skip depending on risk policy.
- If risk-reward is below threshold: no trade.

## Quality Criteria (Completion Checks)
A setup is complete only when all are true:
- Context identified (trend + location + higher timeframe check).
- Pattern family identified (reversal/continuation/indecision).
- Confirmation rule passed.
- Invalidation level and target defined.
- Position size computed from risk, not guesswork.
- Trade journal fields prepared.

## Pattern Shortlist (Use Most)
Use a focused set first, then expand:
- Engulfing (bullish/bearish)
- Hammer / Shooting Star
- Morning Star / Evening Star
- Pin Bar
- Doji / Spinning Top (confirmation required)
- Inside Bar and failed break (fakey)

## References
- [Core pattern notes](./references/core-patterns.md)
