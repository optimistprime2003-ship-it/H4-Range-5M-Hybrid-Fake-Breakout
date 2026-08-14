import requests
import pandas as pd
import logging
import os
import re
from itertools import cycle
from datetime import datetime, timedelta

# =========================================================
# CONFIGURATION
# =========================================================

RANGE_PAIRS = [
    "EUR/USD",
    "AUD/USD",
    "ETH/USD"
    

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
# Dynamically picks up ANY environment variable named
# TD_API_KEY_<number> — not capped at a fixed count. Add as
# many Twelve Data keys as you want (TD_API_KEY_1, _2, _3, ...
# TD_API_KEY_20, whatever) on Render and they're automatically
# included in rotation without touching this file again.
# Each key rotates round-robin, spreading calls across however
# many separate daily credit allowances you've set up.
# =========================================================

def _load_api_keys():

    pattern = re.compile(r"^TD_API_KEY_(\d+)$")

    found = []

    for name, value in os.environ.items():

        match = pattern.match(name)

        if match and value:

            found.append((int(match.group(1)), value))

    # Sorted by the number suffix so rotation order is
    # predictable (TD_API_KEY_1 first, then _2, etc.) — not
    # required for correctness, just easier to reason about
    # when reading logs.
    found.sort(key=lambda pair: pair[0])

    return [key for _, key in found]


keys = _load_api_keys()

key_cycle = cycle(keys) if keys else cycle(["DEMO_KEY"])

# =========================================================
# DAILY RANGE STORAGE
# Persisted externally via data.json — this dict is used
# as a live in-memory cache during a single server session.
# main.py loads it from disk on startup and writes back
# after every scan.
#
# Only the range itself (high/low/date/close-time) is stored
# here. Breakout/reclaim state is NOT persisted across scans —
# every scan recomputes it from scratch by replaying the whole
# day chronologically. See check_strategies() for why.
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

    RANGE REFRESH LOGIC:
    The range is refreshed based on the CALENDAR DATE OF THE ACTUAL
    NY 8AM CANDLE being used, not the server's wall-clock "today".
    Forex markets are closed nights/weekends, so the most recent
    8AM NY candle in the data is almost always genuinely "today's"
    by the time anyone's looking at it — that coincidence hid a bug
    where refresh was keyed off server-today instead of the candle's
    own date. A 24/7 asset (crypto) exposes it immediately: outside
    NY market hours, the most recent available 8AM candle is
    yesterday's, but the old code would still stamp it with today's
    date and then never refresh once today's real candle closed.
    Tying the stored date to the candle's own date makes this
    self-correcting regardless of scan timing or server timezone.

    WHY THIS RE-DERIVES THE WHOLE DAY EVERY SCAN:
    Breakout/reclaim state is not persisted across scans. Every
    scan pulls enough 5-minute history to cover the entire day
    since the range closed, and replays the sequence chronologically
    from scratch, state starting at None. Duplicate signals across
    scans are filtered downstream in main.py against both active
    trades and history.
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
            # FIND THE MOST RECENT NY 8AM CANDLE AVAILABLE
            # =================================================

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

            # The candle's OWN date — this is what determines
            # whether the stored range is stale, not the server's
            # current date.
            target_date = str(target["datetime"]).split(" ")[0]

            stored_date = daily_ranges_ref.get(symbol, {}).get("date")

            # =================================================
            # CREATE / RESET DAILY RANGE
            # Only refreshes when a genuinely newer NY 8AM candle
            # has appeared in the data.
            # =================================================

            if symbol not in daily_ranges_ref or stored_date != target_date:

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

                    "date": target_date,

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
            # =================================================

            breakout_state = None
            breakout_extreme = None

            for i in range(len(eligible_candles)):

                candle = eligible_candles.iloc[i]

                candle_time = str(candle["datetime"])

                close = candle["close"]

                # =============================================
                # NO BREAKOUT CURRENTLY ACTIVE
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

                breakout_state = None
                breakout_extreme = None

        except Exception as e:

            logging.error(
                f"{symbol} Hybrid Fake Breakout strategy error: {e}"
            )

    return signals
