"""
llm/analyzer.py — Send OHLCV candle data to OpenAI ChatGPT for pattern analysis.

Formats candle data into a structured prompt and returns the LLM's
analysis of candlestick patterns, trend direction, and trade suggestions.
"""

from __future__ import annotations

import json
from typing import Any

import openai

from trading_bot import config
from trading_bot.utils.logger import get_logger

log = get_logger(__name__)

# Set API key from config (loaded from .env)
openai.api_key = config.OPENAI_API_KEY

SYSTEM_PROMPT = """You are an expert Indian stock market technical analyst specialising in NIFTY 50 index and options trading. You analyse candlestick chart data to identify patterns, trends, and trading opportunities.

Your analysis should include:
1. **Candlestick Patterns** — Identify any classic patterns (engulfing, hammer, doji, morning/evening star, etc.) in the recent candles.
2. **Trend Analysis** — Is the market in an uptrend, downtrend, or sideways? What's the short-term bias?
3. **Key Levels** — Identify important support and resistance levels from the data.
4. **Volume Analysis** — Note any unusual volume spikes or divergences.
5. **Trade Suggestion** — Based on the patterns and trend, suggest a directional bias (BULLISH / BEARISH / NEUTRAL) with confidence level (HIGH / MEDIUM / LOW).
6. **Risk Warning** — Any caution flags or conflicting signals.

Keep your analysis concise and actionable. Focus on the last 10-20 candles for pattern detection while using the full data for trend context. Use Indian market terminology where appropriate (NIFTY, points, etc.)."""


def _format_candles_for_prompt(candles: list[dict], timeframe: str = "5m") -> str:
    """Format OHLCV candle data into a readable table for the LLM."""
    lines = [
        f"NIFTY 50 — {timeframe} Candles (most recent {len(candles)} bars)",
        f"{'Time':<20} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Volume':>10}",
        "-" * 75,
    ]
    for c in candles:
        ts = c.get("timestamp", c.get("ist", ""))
        # Truncate timestamp for readability
        if len(str(ts)) > 19:
            ts = str(ts)[:19]
        lines.append(
            f"{str(ts):<20} "
            f"{float(c['open']):>10.2f} "
            f"{float(c['high']):>10.2f} "
            f"{float(c['low']):>10.2f} "
            f"{float(c['close']):>10.2f} "
            f"{int(c.get('volume', 0)):>10}"
        )

    # Add summary stats
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    if closes:
        lines.append("")
        lines.append(f"Session High: {max(highs):.2f}")
        lines.append(f"Session Low:  {min(lows):.2f}")
        lines.append(f"Range:        {max(highs) - min(lows):.2f} pts")
        lines.append(f"Last Close:   {closes[-1]:.2f}")
        if len(closes) >= 2:
            chg = closes[-1] - closes[0]
            lines.append(f"Period Change: {chg:+.2f} pts")

    return "\n".join(lines)


def analyze_candles(
    candles: list[dict],
    timeframe: str = "5m",
    extra_context: str = "",
    model: str = "gpt-4o-mini",
) -> dict[str, Any]:
    """
    Send candle data to ChatGPT and return the analysis.

    Args:
        candles: List of OHLCV dicts with keys: timestamp, open, high, low, close, volume
        timeframe: Candle timeframe label (1m, 5m, 15m)
        extra_context: Optional additional context (e.g. existing signals, indicators)
        model: OpenAI model to use

    Returns:
        dict with keys: analysis (str), model (str), candles_sent (int), error (str|None)
    """
    if not config.OPENAI_API_KEY:
        return {
            "analysis": "",
            "model": model,
            "candles_sent": 0,
            "error": "OpenAI API key not configured. Add OPENAI_API_KEY to your .env file.",
        }

    if not candles:
        return {
            "analysis": "",
            "model": model,
            "candles_sent": 0,
            "error": "No candle data provided.",
        }

    # Limit to last 100 candles to stay within token limits
    candles_to_send = candles[-100:]

    data_text = _format_candles_for_prompt(candles_to_send, timeframe)

    user_message = f"Analyse the following NIFTY 50 candlestick data and provide your assessment:\n\n{data_text}"
    if extra_context:
        user_message += f"\n\nAdditional context:\n{extra_context}"

    try:
        client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        analysis = response.choices[0].message.content
        log.info("LLM analysis complete (%s, %d candles)", model, len(candles_to_send))

        return {
            "analysis": analysis,
            "model": model,
            "candles_sent": len(candles_to_send),
            "error": None,
        }
    except openai.AuthenticationError:
        log.error("OpenAI authentication failed — check API key")
        return {
            "analysis": "",
            "model": model,
            "candles_sent": len(candles_to_send),
            "error": "OpenAI authentication failed. Check your API key.",
        }
    except openai.RateLimitError:
        log.warning("OpenAI rate limit hit")
        return {
            "analysis": "",
            "model": model,
            "candles_sent": len(candles_to_send),
            "error": "OpenAI rate limit reached. Try again in a moment.",
        }
    except Exception as exc:
        log.error("LLM analysis failed: %s", exc)
        return {
            "analysis": "",
            "model": model,
            "candles_sent": len(candles_to_send),
            "error": str(exc),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADE POST-MORTEM ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

TRADE_ANALYSIS_SYSTEM_PROMPT = """You are an expert Indian stock market technical analyst and trading coach, specialising in NIFTY 50 intraday options trading. A trader has submitted a completed trade for detailed post-mortem review.

Provide a structured analysis covering exactly these sections:

## 1. ENTRY QUALITY CHECK
- Was the candlestick pattern genuine and reliable at this juncture?
- Was the confirmation candle strong (good body, right direction) or weak (doji, small body)?
- Was the entry at a logical candle structure point, or was it chasing a move?

## 2. INDICATOR CONFLUENCE
Review each indicator at the entry bar and say clearly if it SUPPORTS or OPPOSES the trade direction:
- RSI level and what it signals
- MACD histogram — positive/negative/crossover state  
- Supertrend direction
- EMA (fast vs slow) alignment
- Volume — was the pattern confirmed by above-average volume?
State: "X/5 indicators were aligned — [list which ones supported vs opposed]"

## 3. MARKET CONTEXT AT ENTRY
- Was the overall session trending, ranging, or reversing?
- Was this trade going WITH or AGAINST the dominant trend?
- Were there any key S/R levels nearby that should have warned against entry?
- How did the market move in the candles AFTER the entry — was it a sudden reversal, slow bleed, or sharp gap against the trade?

## 4. ROOT CAUSE OF THE OUTCOME
Be specific — WHY did this particular trade result in this outcome?
Possible reasons: false breakout, counter-trend entry, choppy/sideways market, weak confirmation, early entry before pattern completed, news-driven move, etc.

## 5. WHAT SHOULD HAVE BEEN DONE DIFFERENTLY
Give a clear verdict: **STRONG ENTRY** / **CAUTIOUS** / **SHOULD HAVE SKIPPED** / **CLEAR SKIP**
- What specific red flag(s), if any, should have prevented this entry?
- Was the stop-loss placed correctly relative to the candle structure?
- Any adjustment to entry timing, filter strictness, or trade management that would have helped?

## 6. KEY LESSON
One clear, actionable lesson in 1-2 sentences that the trader can apply to ALL future trades.

Use actual price levels and candle times from the data. Be direct and honest — if the entry was poor quality, say so clearly.

## 7. CONFIG PARAMETER SUGGESTIONS (JSON)
After completing sections 1–6, output a JSON code block with parameter adjustment suggestions.
The block MUST be between ```json and ``` markers.

Only suggest changes for these exact parameter names:
  RSI_BULL_THRESHOLD (current: 55), RSI_BEAR_THRESHOLD (current: 45),
  DUPLICATE_SIGNAL_COOLDOWN (current: 900 sec), SL_BLOCK_DURATION (current: 1200 sec),
  MAX_OPEN_TRADES (current: 1), MAX_DAILY_LOSS (current: 2000),
  VOLUME_EXPANSION_MULT (current: 1.5), INITIAL_SL_POINTS (current: 20)

Format (example):
```json
{"suggestions": [{"param": "RSI_BULL_THRESHOLD", "current": 55, "suggested": 62, "reason": "RSI was only 52 at entry; raising the threshold ensures stronger bullish momentum is confirmed before entry"}]}
```

If no parameter change is warranted, output: `{"suggestions": []}`
Suggest at most 2 parameters. Base suggestions strictly on what the data shows for this trade."""


def _format_trade_context(
    trade: dict,
    candles: list[dict],
    entry_bar_index: int,
    strategy_eval: dict | None,
    timeframe: str,
) -> str:
    """Build the full context block for the trade analysis prompt."""
    lines = []

    # ── Trade details ──────────────────────────────────────────────────────
    opt_type = trade.get("option_type", "IDX")
    strike = trade.get("strike", 0)
    direction = trade.get("direction", "LONG")
    entry_px = trade.get("entry_price") or 0
    exit_px = trade.get("exit_price") or 0
    pnl = trade.get("pnl") or 0
    entry_time = str(trade.get("entry_time", ""))[:19]
    exit_time = str(trade.get("exit_time", ""))[:19]
    exit_reason = trade.get("exit_reason", "UNKNOWN")
    source = trade.get("source", "MANUAL")

    instrument = f"NIFTY {int(strike)} {opt_type}" if opt_type in ("CE", "PE") else "NIFTY INDEX"
    trade_dir = "BULLISH (bought CE — expecting market to RISE)" if opt_type == "CE" else \
                "BEARISH (bought PE — expecting market to FALL)" if opt_type == "PE" else direction

    lines.append("═" * 70)
    lines.append("TRADE DETAILS")
    lines.append("═" * 70)
    lines.append(f"Instrument  : {instrument}")
    lines.append(f"Direction   : {trade_dir}")
    lines.append(f"Entry Time  : {entry_time} IST")
    lines.append(f"Exit Time   : {exit_time} IST")
    lines.append(f"Entry Price : ₹{entry_px:.2f}  (option premium paid)")
    lines.append(f"Exit Price  : ₹{exit_px:.2f}")
    lines.append(f"P&L         : {'₹' + f'{pnl:.2f}' if pnl >= 0 else '-₹' + f'{abs(pnl):.2f}'} ({'PROFIT' if pnl >= 0 else 'LOSS'})")
    lines.append(f"Exit Reason : {exit_reason}")
    lines.append(f"Trade Source: {source}")
    lines.append("")

    # ── NIFTY index candles around entry ──────────────────────────────────
    pre_start = max(0, entry_bar_index - 20)
    pre_end = entry_bar_index + 1  # up to and including entry bar
    pre_candles = candles[pre_start:pre_end]
    post_candles = candles[entry_bar_index + 1: entry_bar_index + 16]  # 15 bars after

    lines.append("═" * 70)
    lines.append(f"NIFTY INDEX {timeframe} CANDLES — PRE-ENTRY CONTEXT (last {len(pre_candles)} bars before/at entry)")
    lines.append("═" * 70)
    lines.append(f"{'Time':<8} {'Open':>9} {'High':>9} {'Low':>9} {'Close':>9} {'Vol':>9}  {'Note'}")
    lines.append("-" * 75)
    for i, c in enumerate(pre_candles):
        ts = str(c.get("ist") or c.get("timestamp", ""))
        if len(ts) > 5:
            ts = ts[-8:-3] if len(ts) >= 8 else ts[-5:]
        bar_note = ""
        # Mark the entry bar
        abs_idx = pre_start + i
        if abs_idx == entry_bar_index:
            bar_note = "  ◄ ENTRY BAR"
        # Mark candle direction
        o, cl = float(c.get("open", 0)), float(c.get("close", 0))
        candle_char = "▲" if cl > o else "▼" if cl < o else "─"
        lines.append(
            f"{str(ts):<8} {float(c.get('open',0)):>9.2f} {float(c.get('high',0)):>9.2f} "
            f"{float(c.get('low',0)):>9.2f} {float(c.get('close',0)):>9.2f} "
            f"{int(c.get('volume',0)):>9}  {candle_char}{bar_note}"
        )

    lines.append("")
    lines.append(f"NIFTY INDEX {timeframe} CANDLES — POST-ENTRY (what happened after entering the trade)")
    lines.append("-" * 75)
    for c in post_candles:
        ts = str(c.get("ist") or c.get("timestamp", ""))
        if len(ts) > 5:
            ts = ts[-8:-3] if len(ts) >= 8 else ts[-5:]
        o, cl = float(c.get("open", 0)), float(c.get("close", 0))
        candle_char = "▲" if cl > o else "▼" if cl < o else "─"
        lines.append(
            f"{str(ts):<8} {float(c.get('open',0)):>9.2f} {float(c.get('high',0)):>9.2f} "
            f"{float(c.get('low',0)):>9.2f} {float(c.get('close',0)):>9.2f} "
            f"{int(c.get('volume',0)):>9}  {candle_char}"
        )
    lines.append("")

    # ── Strategy evaluation results ────────────────────────────────────────
    if strategy_eval:
        lines.append("═" * 70)
        lines.append("STRATEGY ENGINE EVALUATION AT ENTRY")
        lines.append("═" * 70)
        patterns = strategy_eval.get("patterns", [])
        filters = strategy_eval.get("filters", {})
        confirmations = strategy_eval.get("confirmations", 0)
        action = strategy_eval.get("action", "UNKNOWN")
        reason = strategy_eval.get("reason", "")
        strength = strategy_eval.get("strength", 0)
        sl_pts = strategy_eval.get("sl_points", 0)
        tgt_pts = strategy_eval.get("target_points", 0)

        lines.append(f"Pattern(s) Detected : {', '.join(patterns) if patterns else 'None'}")
        lines.append(f"Signal Action       : {action}")
        lines.append(f"Signal Strength     : {strength}/100")
        lines.append(f"Confirmations       : {confirmations}/5")
        lines.append(f"Suggested SL        : {sl_pts:.1f} pts from entry")
        lines.append(f"Suggested Target    : {tgt_pts:.1f} pts from entry")
        lines.append(f"Reason              : {reason}")
        lines.append("")
        lines.append("Indicator Results:")
        for fname, passed in filters.items():
            status = "✓ PASS" if passed else "✗ FAIL"
            lines.append(f"  {fname.replace('_',' ').title():<20}: {status}")
    else:
        lines.append("═" * 70)
        lines.append("STRATEGY ENGINE: No evaluation data available for this trade.")
        lines.append("This may be a manually placed trade or data was not recorded.")
    lines.append("")

    # ── Session summary stats ──────────────────────────────────────────────
    if candles:
        closes = [float(c.get("close", 0)) for c in candles]
        highs = [float(c.get("high", 0)) for c in candles]
        lows = [float(c.get("low", 0)) for c in candles]
        lines.append("SESSION SUMMARY (full day context)")
        lines.append(f"  Day High : {max(highs):.2f}")
        lines.append(f"  Day Low  : {min(lows):.2f}")
        lines.append(f"  Day Range: {max(highs) - min(lows):.2f} pts")
        lines.append(f"  Open     : {closes[0]:.2f}  |  Last Close: {closes[-1]:.2f}")
        overall_chg = closes[-1] - closes[0]
        lines.append(f"  Net Move : {overall_chg:+.2f} pts ({'Up day ▲' if overall_chg > 5 else 'Down day ▼' if overall_chg < -5 else 'Flat day ─'})")
    lines.append("")

    return "\n".join(lines)


def analyze_failed_trade(
    trade: dict,
    candles: list[dict],
    entry_bar_index: int,
    strategy_eval: dict | None = None,
    timeframe: str = "5m",
    model: str = "gpt-4o-mini",
) -> dict[str, Any]:
    """
    LLM post-mortem analysis of a completed trade.

    Args:
        trade: Trade record dict (entry_price, exit_price, pnl, entry_time,
               exit_time, exit_reason, option_type, strike, symbol, source)
        candles: NIFTY index OHLCV candle list for the trade day
        entry_bar_index: index in `candles` of the bar at trade entry time
        strategy_eval: Optional dict from strategy.evaluate() with patterns/indicators
        timeframe: candle timeframe label (e.g. "5m")
        model: OpenAI model to use

    Returns:
        dict with keys: analysis (str), model (str), error (str|None)
    """
    if not config.OPENAI_API_KEY:
        return {
            "analysis": "",
            "model": model,
            "error": "OpenAI API key not configured.",
        }

    model = model.strip()  # guard against \r\n from env vars

    context_text = _format_trade_context(trade, candles, entry_bar_index, strategy_eval, timeframe)
    user_message = (
        "Please provide a detailed post-mortem analysis of this trade:\n\n"
        + context_text
        + "\nAnalyse this trade thoroughly using the six sections in your instructions."
    )

    import time as _time

    for _attempt in range(3):
        try:
            client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": TRADE_ANALYSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.3,
                max_tokens=2400,
            )
            analysis = response.choices[0].message.content
            log.info("Trade analysis complete (%s, trade=%s)", model, trade.get("trade_id", "?"))

            # ── Parse JSON suggestions block from the response ─────────────────
            import re as _re
            import json as _json_mod
            suggestions: list = []
            try:
                json_match = _re.search(r'```json\s*(\{.*?\})\s*```', analysis, _re.DOTALL)
                if json_match:
                    parsed = _json_mod.loads(json_match.group(1))
                    suggestions = parsed.get("suggestions", [])
                    # Strip the JSON block from the narrative text
                    analysis = _re.sub(r'\n*## 7\..*?```json.*?```', '', analysis, flags=_re.DOTALL).strip()
            except Exception as _parse_exc:
                log.debug("Suggestions JSON parse: %s", _parse_exc)

            return {"analysis": analysis, "suggestions": suggestions, "model": model, "error": None}

        except openai.AuthenticationError:
            return {"analysis": "", "model": model, "error": "OpenAI authentication failed. Check your API key."}
        except openai.RateLimitError:
            if _attempt < 2:
                _time.sleep(5 * (_attempt + 1))   # 5s, then 10s
                continue
            return {"analysis": "", "model": model, "error": "OpenAI rate limit reached. Please try again in ~30 seconds."}
        except Exception as exc:
            log.error("Trade analysis LLM error: %s", exc)
            return {"analysis": "", "model": model, "error": str(exc)}
