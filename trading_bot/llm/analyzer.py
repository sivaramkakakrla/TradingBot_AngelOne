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
