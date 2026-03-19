"""Quick diagnostic: check auth + today's candle data."""
from trading_bot.auth.login import get_session
from trading_bot.utils.time_utils import now_ist
from trading_bot import config

session = get_session()
print(f"Session: {'OK' if session else 'NONE'}")
print(f"Time: {now_ist()}")

today = now_ist()
from_str = today.strftime("%Y-%m-%d 09:15")
to_str = today.strftime("%Y-%m-%d %H:%M")
print(f"From: {from_str}")
print(f"To: {to_str}")

try:
    ltp_resp = session.ltpData(
        exchange=config.EXCHANGE,
        tradingsymbol=config.UNDERLYING,
        symboltoken=config.NIFTY_TOKEN,
    )
    print(f"LTP response: {ltp_resp}")
except Exception as e:
    print(f"LTP error: {e}")

try:
    resp = session.getCandleData({
        "exchange": config.EXCHANGE,
        "symboltoken": config.NIFTY_TOKEN,
        "interval": "ONE_MINUTE",
        "fromdate": from_str,
        "todate": to_str,
    })
    if resp:
        status = resp.get("status")
        data = resp.get("data") or []
        msg = resp.get("message", "")
        print(f"Candle status={status}, bars={len(data)}, msg={msg}")
        if data:
            print(f"First bar: {data[0]}")
            print(f"Last bar: {data[-1]}")
    else:
        print(f"Empty response: {resp}")
except Exception as e:
    print(f"Candle error: {e}")
