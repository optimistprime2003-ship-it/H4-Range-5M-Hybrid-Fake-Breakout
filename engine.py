import requests
import pandas as pd
import logging
import os
from itertools import cycle
from datetime import datetime, timedelta

# =========================================================
# CONFIGURATION
# =========================================================

RANGE_PAIRS = [
    "EUR/USD",
    "AUD/USD",
    "ETH/USD",
    "BTC/USD"
]

NY_SESSION_START = "08:00:00"

# The first 4H candle runs 08:00–12:00 NY. The range isn't
# valid/tradable until that candle has fully closed.
RANGE_DURATION_HOURS = 4

# How far back to pull 5-minute candles so a single scan can
# always see the entire trading day since the range closed,
# not just a short rolling window. 288 candles = 24 hours.
FIVE_MIN_LOOKBACK = 288

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
#
# Only the range itself (high/low/date/close-time) is stored
# here now. Breakout/reclaim state is NOT persisted across
# scans anymore — every scan recomputes it from scratch by
# replaying the whole day chronologically. See check_strategies()
# for why the old persisted-state approach was unsafe.
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
    the day's range (high/low) is preserved across scans and
    persisted externally by main.py.

    H4 RANGE + 5M HYBRID FAKE BREAKOUT
    -----------------------------------
    Rules, matched to the source strategy:
      1. Mark the high/low of the first 4H candle of the NY session.
      2. On the 5-minute chart, wait for a candle to CLOSE outside
         the range (wick-only doesn't count).
      3. Once outside, keep tracking — if later candles keep
         closing further outside, the breakout extreme keeps
         updating. As soon as a candle CLOSES back inside the
         range, that's the reclaim/entry trigger.
      4. Stop loss goes at the exact extreme (high/low) reached
         during the breakout move, not at the range line itself.
      5. Take profit is 2x the stop-loss distance.
      6. Multiple trades can happen in the same day if price
         re-enters and gives another valid breakout+reclaim.

    IMPORTANT — WHY THIS RE-DERIVES THE WHOLE DAY EVERY SCAN:
    An earlier version tried to persist "we're currently mid-
    breakout" state across scans while only re-checking the last
    ~100 minutes of candles each time. That's unsafe: the start of
    that short rolling window almost always contains candles from
    BEFORE the breakout even began, but the code would treat them
    as if the breakout was already active — which could misfire on
    a stale old candle, or clear the breakout state prematurely,
    before ever reaching the real, current reclaim candle. That's
    a plausible explanation for signals silently not firing.

    The fix: don't persist breakout/reclaim state across scans at
    all. Every scan pulls enough 5-minute history to cover the
    entire day since the range closed, and replays the sequence
    chronologically from scratch, state starting at None. This is
    deterministic and self-correcting regardless of scan timing.
    Duplicate signals across scans are filtered downstream in
    main.py against both active trades and history.
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
                outputsize=FIVE_MIN_LOOKBACK
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

                # The range isn't valid/tradable until this 4H
                # candle has fully closed — i.e. 4 hours after
                # it opened.
                range_open_dt = datetime.strptime(
                    str(target["datetime"]),
                    "%Y-%m-%d %H:%M:%S"
                )

                range_close_dt = range_open_dt + timedelta(
                    hours=RANGE_DURATION_HOURS
                )

                daily_ranges_ref[symbol] = {

                    "date": today,

                    "high": float(target["high"]),

                    "low": float(target["low"]),

                    "range_close_time": range_close_dt.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                }

                logging.info(
                    f"{symbol} NY Range Set | "
                    f"HIGH={target['high']} "
                    f"LOW={target['low']} "
                    f"VALID FROM={daily_ranges_ref[symbol]['range_close_time']}"
                )

            # =================================================
            # FIXED RANGE VALUES
            # =================================================

            range_high = daily_ranges_ref[symbol]["high"]

            range_low = daily_ranges_ref[symbol]["low"]

            range_close_time = daily_ranges_ref[symbol].get(
                "range_close_time"
            )

            if range_close_time is None:
                # Data from before this field existed — skip until
                # the next daily reset regenerates it cleanly.
                continue

            # =================================================
            # CANDLES SINCE THE RANGE BECAME VALID
            # Only candles at or after the 4H range candle's
            # close are eligible — anything earlier happened
            # before the range even existed.
            # =================================================

            eligible_candles = df_5m[
                df_5m["datetime"] >= range_close_time
            ].reset_index(drop=True)

            if eligible_candles.empty:
                continue

            # =================================================
            # REPLAY THE FULL SEQUENCE CHRONOLOGICALLY
            # State always starts at None here — nothing is
            # carried in from a previous scan. Every valid
            # breakout+reclaim pair found gets appended; the
            # loop keeps going afterward so multiple trades in
            # the same day are all detected in a single pass.
            # =================================================

            breakout_state = None
            breakout_extreme = None

            for i in range(len(eligible_candles)):

                candle = eligible_candles.iloc[i]

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
                        breakout_extreme = float(candle["high"])

                    elif close < range_low:

                        breakout_state = "outside_below"
                        breakout_extreme = float(candle["low"])

                    continue

                # =============================================
                # BREAKOUT ACTIVE — STILL EXTENDING
                # =============================================

                if breakout_state == "outside_above" and close > range_high:

                    breakout_extreme = max(
                        breakout_extreme,
                        float(candle["high"])
                    )

                    continue

                if breakout_state == "outside_below" and close < range_low:

                    breakout_extreme = min(
                        breakout_extreme,
                        float(candle["low"])
                    )

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

                # Reset and keep scanning for another setup later
                # in the same day.
                breakout_state = None
                breakout_extreme = None

        except Exception as e:

            logging.error(
                f"{symbol} Hybrid Fake Breakout strategy error: {e}"
            )

    return signals
