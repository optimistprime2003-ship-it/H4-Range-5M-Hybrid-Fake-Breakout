import requests
import pandas as pd
import logging
import os
from itertools import cycle
from datetime import datetime

# =========================================================
# CONFIGURATION
# =========================================================

RANGE_PAIRS = [
    "EUR/USD",
    "AUD/USD"
]

NY_SESSION_START = "08:00:00"

# =========================================================
# API ROTATION
# =========================================================

keys = [
    os.getenv(f"TD_API_KEY_{i}")
    for i in range(1, 5)
    if os.getenv(f"TD_API_KEY_{i}")
]

key_cycle = cycle(keys) if keys else cycle(["DEMO_KEY"])

# =========================================================
# DAILY RANGE STORAGE
# Persisted externally via data.json — this dict is used
# as a live in-memory cache during a single server session.
# main.py loads it from disk on startup and writes back
# after every scan.
# =========================================================

daily_ranges = {}

# =========================================================
# DATA FETCHER
# =========================================================

def get_data(symbol, interval, outputsize=50):

    current_key = next(key_cycle)

    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={symbol}"
        f"&interval={interval}"
        f"&outputsize={outputsize}"
        f"&apikey={current_key}"
        f"&timezone=America/New_York"
    )

    try:

        res = requests.get(url, timeout=10).json()

        if "values" not in res:

            logging.error(
                f"No values returned for {symbol} {interval} — "
                f"API response: {res}"
            )

            return None

        df = pd.DataFrame(res["values"])

        df[["open", "high", "low", "close"]] = (
            df[["open", "high", "low", "close"]].astype(float)
        )

        df = df.iloc[::-1].reset_index(drop=True)

        return df

    except Exception as e:

        logging.error(f"Data fetch error [{symbol} {interval}]: {e}")

        return None

# =========================================================
# MAIN STRATEGY ENGINE
# =========================================================

def check_strategies(daily_ranges_ref):
    """
    Accepts the shared daily_ranges dict by reference so that
    breakout state is preserved across scans and persisted
    externally by main.py.

    H4 RANGE + 5M HYBRID FAKE BREAKOUT
    -----------------------------------
    Rules, matched to the source strategy:
      1. Mark the high/low of the first 4H candle of the NY session.
      2. On the 5-minute chart, wait for a candle to CLOSE outside
         the range (wick-only doesn't count).
      3. Once outside, keep tracking — if later candles keep
         closing further outside, the breakout extreme keeps
         updating. As soon as a candle CLOSES back inside the
         range, that's the reclaim/entry trigger. The reclaim
         does not have to be the very next candle after the
         *original* breakout — it just has to be the first candle
         whose close comes back inside after a run of outside-closes.
      4. Stop loss goes at the exact extreme (high/low) reached
         during the breakout move, not at the range line itself.
      5. Take profit is 2x the stop-loss distance.
    """

    signals = []

    for symbol in RANGE_PAIRS:

        try:

            # =================================================
            # FETCH MARKET DATA
            # =================================================

            df_4h = get_data(
                symbol,
                "4h",
                outputsize=20
            )

            df_5m = get_data(
                symbol,
                "5min",
                outputsize=100
            )

            if df_4h is None or df_5m is None:
                continue

            if df_4h.empty or df_5m.empty:
                continue

            # =================================================
            # DATE
            # =================================================

            today = datetime.now().strftime("%Y-%m-%d")

            # =================================================
            # CREATE / RESET DAILY RANGE
            # =================================================

            if (
                symbol not in daily_ranges_ref
                or daily_ranges_ref[symbol]["date"] != today
            ):

                session_data = df_4h[
                    df_4h["datetime"]
                    .str.contains(NY_SESSION_START)
                ]

                if session_data.empty:

                    logging.warning(
                        f"No NY open candle found for {symbol} "
                        f"— skipping range setup."
                    )

                    continue

                target = session_data.iloc[-1]

                daily_ranges_ref[symbol] = {

                    "date": today,

                    "high": float(target["high"]),

                    "low": float(target["low"]),

                    # Time of the most recent candle that kept
                    # price closing outside the range (the active
                    # edge of the breakout run).
                    "breakout_state": None,

                    "breakout_candle_time": None,

                    # The furthest high/low reached during the
                    # current breakout run — used as the stop loss
                    # once a reclaim/entry fires.
                    "breakout_extreme": None
                }

                logging.info(
                    f"{symbol} NY Range Set | "
                    f"HIGH={target['high']} "
                    f"LOW={target['low']}"
                )

            # =================================================
            # FIXED RANGE VALUES
            # =================================================

            range_high = daily_ranges_ref[symbol]["high"]

            range_low = daily_ranges_ref[symbol]["low"]

            breakout_state = daily_ranges_ref[symbol].get(
                "breakout_state",
                None
            )

            breakout_extreme = daily_ranges_ref[symbol].get(
                "breakout_extreme",
                None
            )

            # =================================================
            # RECENT CANDLES — last 20 5-minute candles
            # =================================================

            recent_candles = df_5m.tail(20).reset_index(drop=True)

            # =================================================
            # LOOP CANDLES
            # Every candle while a breakout is active is either:
            #   (a) still closing outside the range -> extends
            #       the breakout run and updates the extreme, or
            #   (b) closes back inside the range -> reclaim/entry.
            # There is no third "ignore this candle" state once a
            # breakout is active, so a reclaim candle is never
            # missed just because its wick also pierced the range.
            # =================================================

            for i in range(len(recent_candles)):

                candle = recent_candles.iloc[i]

                candle_time = str(candle["datetime"])

                close = candle["close"]

                # =============================================
                # NO BREAKOUT CURRENTLY ACTIVE
                # Only a confirmed body CLOSE outside the range
                # starts a breakout. A wick that pierces the level
                # without a close beyond it is ignored, per the
                # "wicks alone don't count" rule.
                # =============================================

                if breakout_state is None:

                    if close > range_high:

                        breakout_state = "outside_above"
                        breakout_candle_time = candle_time
                        breakout_extreme = float(candle["high"])

                    elif close < range_low:

                        breakout_state = "outside_below"
                        breakout_candle_time = candle_time
                        breakout_extreme = float(candle["low"])

                    else:

                        continue

                    daily_ranges_ref[symbol]["breakout_state"] = breakout_state
                    daily_ranges_ref[symbol]["breakout_candle_time"] = breakout_candle_time
                    daily_ranges_ref[symbol]["breakout_extreme"] = breakout_extreme

                    continue

                # =============================================
                # BREAKOUT ACTIVE — STILL EXTENDING
                # Price keeps closing further outside the range.
                # Update the tracked extreme and keep waiting.
                # =============================================

                if breakout_state == "outside_above" and close > range_high:

                    breakout_candle_time = candle_time

                    breakout_extreme = max(
                        breakout_extreme,
                        float(candle["high"])
                    )

                    daily_ranges_ref[symbol]["breakout_candle_time"] = breakout_candle_time
                    daily_ranges_ref[symbol]["breakout_extreme"] = breakout_extreme

                    continue

                if breakout_state == "outside_below" and close < range_low:

                    breakout_candle_time = candle_time

                    breakout_extreme = min(
                        breakout_extreme,
                        float(candle["low"])
                    )

                    daily_ranges_ref[symbol]["breakout_candle_time"] = breakout_candle_time
                    daily_ranges_ref[symbol]["breakout_extreme"] = breakout_extreme

                    continue

                # =============================================
                # BREAKOUT ACTIVE — CLOSE BACK INSIDE THE RANGE
                # This is the reclaim/entry trigger, regardless
                # of whether this candle's wick also poked past
                # the boundary again — only the close matters.
                # =============================================

                if breakout_state == "outside_above":

                    entry = close

                    sl = breakout_extreme

                    risk = abs(sl - entry)

                    tp = entry - (risk * 2)

                    signals.append({

                        "symbol": symbol,

                        "type": "SELL",

                        "entry": round(entry, 5),

                        "sl": round(sl, 5),

                        "tp": round(tp, 5),

                        "rr": "1:2",

                        "strat": "Hybrid Fake Breakout",

                        "time": candle_time
                    })

                    daily_ranges_ref[symbol]["breakout_state"] = None
                    daily_ranges_ref[symbol]["breakout_candle_time"] = None
                    daily_ranges_ref[symbol]["breakout_extreme"] = None

                    breakout_state = None
                    breakout_extreme = None

                    break

                elif breakout_state == "outside_below":

                    entry = close

                    sl = breakout_extreme

                    risk = abs(entry - sl)

                    tp = entry + (risk * 2)

                    signals.append({

                        "symbol": symbol,

                        "type": "BUY",

                        "entry": round(entry, 5),

                        "sl": round(sl, 5),

                        "tp": round(tp, 5),

                        "rr": "1:2",

                        "strat": "Hybrid Fake Breakout",

                        "time": candle_time
                    })

                    daily_ranges_ref[symbol]["breakout_state"] = None
                    daily_ranges_ref[symbol]["breakout_candle_time"] = None
                    daily_ranges_ref[symbol]["breakout_extreme"] = None

                    breakout_state = None
                    breakout_extreme = None

                    break

        except Exception as e:

            logging.error(
                f"{symbol} Hybrid Fake Breakout strategy error: {e}"
            )

    return signals
