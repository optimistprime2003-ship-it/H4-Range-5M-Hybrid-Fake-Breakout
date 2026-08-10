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
    """

    signals = []

    # =====================================================
    # H4 RANGE + 5M HYBRID FAKE BREAKOUT STRATEGY
    # =====================================================

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

                    # Stores the datetime string of the breakout
                    # candle instead of an array index so that
                    # the reclaim check survives across scans.
                    "breakout_state": None,

                    "breakout_candle_time": None
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

            breakout_candle_time = daily_ranges_ref[symbol].get(
                "breakout_candle_time",
                None
            )

            # =================================================
            # RECENT CANDLES — last 20 5-minute candles
            # =================================================

            recent_candles = df_5m.tail(20).reset_index(drop=True)

            # =================================================
            # LOOP CANDLES
            # =================================================

            for i in range(len(recent_candles)):

                candle = recent_candles.iloc[i]

                candle_time = str(candle["datetime"])

                # =============================================
                # BODY BREAKS — close outside the range
                # =============================================

                bullish_body_break = (
                    candle["close"] > range_high
                )

                bearish_body_break = (
                    candle["close"] < range_low
                )

                # =============================================
                # WICK-ONLY REJECTION
                # Wick pierces the level but body closes inside
                # =============================================

                wick_break_high = (
                    candle["high"] > range_high
                    and candle["close"] <= range_high
                )

                wick_break_low = (
                    candle["low"] < range_low
                    and candle["close"] >= range_low
                )

                # =============================================
                # STORE BREAKOUT STATE
                # We record the candle's datetime string so
                # that the "next candle" check is time-based,
                # not index-based, and survives server restarts.
                # =============================================

                if bullish_body_break:

                    breakout_state = "outside_above"

                    breakout_candle_time = candle_time

                    daily_ranges_ref[symbol][
                        "breakout_state"
                    ] = breakout_state

                    daily_ranges_ref[symbol][
                        "breakout_candle_time"
                    ] = breakout_candle_time

                    continue

                elif bearish_body_break:

                    breakout_state = "outside_below"

                    breakout_candle_time = candle_time

                    daily_ranges_ref[symbol][
                        "breakout_state"
                    ] = breakout_state

                    daily_ranges_ref[symbol][
                        "breakout_candle_time"
                    ] = breakout_candle_time

                    continue

                # =============================================
                # IGNORE PURE WICK SWEEPS
                # =============================================

                elif wick_break_high or wick_break_low:

                    continue

                # =============================================
                # RECLAIM CHECK
                # The reclaim candle must be the candle
                # immediately after the breakout candle.
                # We verify this by finding the breakout candle's
                # position in the current window and confirming
                # this candle is exactly one step after it.
                # =============================================

                if breakout_state is not None and breakout_candle_time is not None:

                    # Find where the breakout candle sits in
                    # the current window by matching its datetime.
                    breakout_positions = recent_candles.index[
                        recent_candles["datetime"].astype(str) == breakout_candle_time
                    ].tolist()

                    if not breakout_positions:
                        # Breakout candle has scrolled out of
                        # the 20-candle window — reset state.
                        daily_ranges_ref[symbol]["breakout_state"] = None
                        daily_ranges_ref[symbol]["breakout_candle_time"] = None
                        breakout_state = None
                        breakout_candle_time = None
                        continue

                    breakout_pos = breakout_positions[0]

                    is_next_candle = (i == breakout_pos + 1)

                else:

                    is_next_candle = False

                # =============================================
                # INSTANT RECLAIM CONDITIONS
                # =============================================

                sell_reentry = (
                    breakout_state == "outside_above"
                    and is_next_candle
                    and candle["close"] < range_high
                )

                buy_reentry = (
                    breakout_state == "outside_below"
                    and is_next_candle
                    and candle["close"] > range_low
                )

                # =============================================
                # SELL SIGNAL
                # =============================================

                if sell_reentry:

                    entry = candle["close"]

                    sl = range_high

                    risk = abs(sl - entry)

                    tp = entry - (risk * 2)

                    signals.append({

                        "symbol": symbol,

                        "type": "SELL",

                        "entry": round(entry, 2),

                        "sl": round(sl, 2),

                        "tp": round(tp, 2),

                        "rr": "1:2",

                        "strat": "Hybrid Fake Breakout",

                        "time": candle_time
                    })

                    daily_ranges_ref[symbol]["breakout_state"] = None
                    daily_ranges_ref[symbol]["breakout_candle_time"] = None

                    break

                # =============================================
                # BUY SIGNAL
                # =============================================

                elif buy_reentry:

                    entry = candle["close"]

                    sl = range_low

                    risk = abs(entry - sl)

                    tp = entry + (risk * 2)

                    signals.append({

                        "symbol": symbol,

                        "type": "BUY",

                        "entry": round(entry, 2),

                        "sl": round(sl, 2),

                        "tp": round(tp, 2),

                        "rr": "1:2",

                        "strat": "Hybrid Fake Breakout",

                        "time": candle_time
                    })

                    daily_ranges_ref[symbol]["breakout_state"] = None
                    daily_ranges_ref[symbol]["breakout_candle_time"] = None

                    break

        except Exception as e:

            logging.error(
                f"{symbol} Hybrid Fake Breakout strategy error: {e}"
            )

    return signals
