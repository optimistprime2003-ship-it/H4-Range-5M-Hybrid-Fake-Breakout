import os
import json
import logging
import threading
import statistics
import uvicorn
import requests
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
import engine
from datetime import datetime, timedelta
from pywebpush import webpush, WebPushException

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = FastAPI()

# =========================================================
# DATA STORAGE LOCATION
# If DATA_DIR is set (pointing at a Render persistent disk
# mount path, e.g. /var/data), data.json lives there and
# survives redeploys. If unset, falls back to the working
# directory — same behavior as before, so nothing breaks if
# a disk hasn't been attached yet.
# =========================================================

DATA_DIR = os.getenv("DATA_DIR", ".")

DB_FILE = os.path.join(DATA_DIR, "data.json")

# =========================================================
# SCAN INTERVAL
# The scheduler (not the dashboard route) drives scanning now.
# =========================================================

SCAN_INTERVAL_MINUTES = 5

# =========================================================
# ACTIVE SCANNING WINDOW
# The 4H NY range candle opens at 8:00 AM NY — before that,
# there is nothing new for the strategy to find, so full scans
# only start once NY hits this hour. This is the single biggest
# source of wasted Twelve Data API credits: scanning every 5
# minutes around the clock burns through the daily credit cap
# long before it's actually useful. zoneinfo handles NY's
# daylight-saving shift automatically, so this stays correct
# year-round without manual adjustment.
# =========================================================

NY_TZ = ZoneInfo("America/New_York")

# WAT — West Africa Time (Nigeria), UTC+1 year-round, no DST.
WAT_TZ = ZoneInfo("Africa/Lagos")

ACTIVE_WINDOW_START_HOUR = 9


def is_within_active_window():
    return datetime.now(NY_TZ).hour >= ACTIVE_WINDOW_START_HOUR


def format_dual_time(ny_time_str):
    """
    Every signal's stored "time" is already in NY local time (the
    candle timestamp from Twelve Data). This converts it to both
    a readable NY string and its WAT (Nigeria) equivalent, handling
    NY's daylight-saving shift automatically via zoneinfo.
    """

    try:

        naive = datetime.strptime(ny_time_str, "%Y-%m-%d %H:%M:%S")

        ny_dt = naive.replace(tzinfo=NY_TZ)

        wat_dt = ny_dt.astimezone(WAT_TZ)

        return {
            "ny": ny_dt.strftime("%b %d, %I:%M %p") + " NY",
            "wat": wat_dt.strftime("%b %d, %I:%M %p") + " WAT"
        }

    except Exception:

        return {"ny": ny_time_str, "wat": "-"}


def ny_str_to_unix(ny_time_str):
    """
    Converts a stored NY-local candle timestamp into a UTC unix
    timestamp (seconds) — the format Lightweight Charts expects.
    """

    try:

        naive = datetime.strptime(ny_time_str, "%Y-%m-%d %H:%M:%S")

        ny_dt = naive.replace(tzinfo=NY_TZ)

        return int(ny_dt.timestamp())

    except Exception:

        return None

# =========================================================
# TELEGRAM
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_alert(message):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:

        logging.warning(
            "Telegram alert skipped — TELEGRAM_BOT_TOKEN or "
            "TELEGRAM_CHAT_ID not set."
        )

        return

    try:

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )

    except Exception as e:

        logging.error(f"Telegram alert failed: {e}")

# =========================================================
# BROWSER PUSH NOTIFICATIONS (Web Push)
# Separate from Telegram entirely — each device that opts in
# holds its own subscription (stored in data.json). All three
# users are on Android, so this works directly in Chrome with
# no PWA-install requirement (that's an iOS-only restriction).
# =========================================================

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:admin@example.com")


def send_push_notification(subscription_info, title, body):
    """
    Sends one push message to one subscribed device. Returns
    "expired" if the subscription is no longer valid (device
    unsubscribed, cleared browser data, etc.) so the caller can
    prune it from storage — otherwise returns True/False.
    """

    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:

        logging.warning(
            "Push notification skipped — VAPID keys not set."
        )

        return False

    try:

        webpush(
            subscription_info=subscription_info,
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_CLAIMS_EMAIL}
        )

        return True

    except WebPushException as e:

        status = getattr(e.response, "status_code", None)

        if status in (404, 410):
            # Subscription is dead (device unsubscribed, browser
            # data cleared, etc.) — tell the caller to drop it.
            return "expired"

        logging.error(f"Push notification failed: {e}")

        return False

    except Exception as e:

        logging.error(f"Push notification failed: {e}")

        return False


def send_push_to_all(db, title, body):
    """
    Sends to every subscribed device and prunes any that came
    back expired, directly on the passed-in db dict (the caller
    is responsible for saving it afterward).
    """

    subs = db.get("push_subscriptions", [])

    still_valid = []

    for sub in subs:

        result = send_push_notification(sub, title, body)

        if result != "expired":
            still_valid.append(sub)

    db["push_subscriptions"] = still_valid

# =========================================================
# CONCURRENCY LOCK
# Guards every read/write of data.json AND engine.daily_ranges,
# since the scheduler thread and any web request both touch them.
# =========================================================

data_lock = threading.Lock()

# =========================================================
# LOAD DATABASE
# Reads data.json, rehydrates daily_ranges into engine memory,
# and computes all analytics stats fresh from history.
# =========================================================

def load_data():

    if os.path.exists(DB_FILE):

        try:

            with open(DB_FILE, "r") as f:
                db = json.load(f)

            if "active" not in db:
                db["active"] = []

            if "history" not in db:
                db["history"] = []

            if "daily_ranges" not in db:
                db["daily_ranges"] = {}

            if "settings" not in db:
                db["settings"] = {"telegram_alerts": True}

            if "last_scan" not in db:
                db["last_scan"] = None

            if "push_subscriptions" not in db:
                db["push_subscriptions"] = []

            # Rehydrate engine's in-memory daily_ranges from disk
            # so today's range survives server restarts.
            engine.daily_ranges.clear()
            engine.daily_ranges.update(db["daily_ranges"])

            # =================================================
            # ADVANCED STATS ENGINE
            # =================================================

            stats = {

                "wins": 0,
                "losses": 0,
                "total": 0,

                "rr_won": 0.0,
                "rr_lost": 0.0,
                "net_rr": 0.0,

                "profit_factor": 0.0,
                "expectancy": 0.0,

                "sharpe": None,
                "sortino": None,

                "pairs": {},
                "strategies": {},
                "equity_curve": []
            }

            # R-multiple for every closed trade (WIN = +rr_value,
            # LOSS = -1.0), used for Sharpe/Sortino below.
            r_multiples = []

            # =================================================
            # PROCESS HISTORY
            # =================================================

            for s in db.get("history", []):

                symbol = s.get("symbol", "UNKNOWN")

                strategy = s.get("strat", "UNKNOWN")

                rr_text = str(s.get("rr", "1:1"))

                stats["total"] += 1

                # -----------------------------------------
                # EXTRACT RR VALUE
                # -----------------------------------------

                try:

                    rr_value = float(rr_text.split(":")[1])

                except (ValueError, IndexError):

                    rr_value = 1.0

                # -----------------------------------------
                # PAIR STATS
                # -----------------------------------------

                if symbol not in stats["pairs"]:

                    stats["pairs"][symbol] = {

                        "wins": 0,
                        "losses": 0,
                        "total": 0,
                        "rr": 0.0
                    }

                stats["pairs"][symbol]["total"] += 1

                # -----------------------------------------
                # STRATEGY STATS
                # -----------------------------------------

                if strategy not in stats["strategies"]:

                    stats["strategies"][strategy] = {

                        "wins": 0,
                        "losses": 0,
                        "total": 0
                    }

                stats["strategies"][strategy]["total"] += 1

                # -----------------------------------------
                # WINS
                # -----------------------------------------

                if s.get("result") == "WIN":

                    stats["wins"] += 1

                    stats["rr_won"] += rr_value

                    stats["pairs"][symbol]["wins"] += 1

                    stats["pairs"][symbol]["rr"] += rr_value

                    stats["strategies"][strategy]["wins"] += 1

                    r_multiples.append(rr_value)

                # -----------------------------------------
                # LOSSES
                # -----------------------------------------

                elif s.get("result") == "LOSS":

                    stats["losses"] += 1

                    stats["rr_lost"] += 1.0

                    stats["pairs"][symbol]["losses"] += 1

                    stats["pairs"][symbol]["rr"] -= 1.0

                    stats["strategies"][strategy]["losses"] += 1

                    r_multiples.append(-1.0)

            # =================================================
            # FINAL METRICS
            # =================================================

            stats["net_rr"] = round(
                stats["rr_won"] - stats["rr_lost"],
                2
            )

            if stats["rr_lost"] > 0:

                stats["profit_factor"] = round(
                    stats["rr_won"] / stats["rr_lost"],
                    2
                )

            else:

                stats["profit_factor"] = round(
                    stats["rr_won"],
                    2
                )

            if stats["total"] > 0:

                stats["expectancy"] = round(
                    stats["net_rr"] / stats["total"],
                    2
                )

            # =================================================
            # SHARPE / SORTINO (trade-based, R-multiple variant)
            # =================================================

            n = len(r_multiples)

            if n >= 2:

                mean_r = statistics.mean(r_multiples)

                stdev_r = statistics.stdev(r_multiples)

                if stdev_r > 0:

                    stats["sharpe"] = round(mean_r / stdev_r, 2)

                downside_sq_sum = sum(
                    min(r, 0) ** 2 for r in r_multiples
                )

                downside_dev = (downside_sq_sum / n) ** 0.5

                if downside_dev > 0:

                    stats["sortino"] = round(mean_r / downside_dev, 2)

            stats["sample_size"] = n

            # =================================================
            # EQUITY CURVE
            # =================================================

            closed_trades = [
                s for s in db.get("history", [])
                if s.get("closed_at") and s.get("result") in ("WIN", "LOSS")
            ]

            closed_trades.sort(key=lambda s: s["closed_at"])

            running_rr = 0.0

            equity_points = []

            for s in closed_trades:

                rr_text = str(s.get("rr", "1:1"))

                try:

                    rr_value = float(rr_text.split(":")[1])

                except (ValueError, IndexError):

                    rr_value = 1.0

                if s.get("result") == "WIN":

                    running_rr += rr_value

                else:

                    running_rr -= 1.0

                equity_points.append({

                    "time": s["closed_at"],

                    "rr": round(running_rr, 2)
                })

            stats["equity_curve"] = equity_points

            # =================================================
            # DAILY HEATMAP
            # Net RR per calendar day (grouped by the date portion
            # of closed_at) for the last 35 days, oldest first —
            # feeds the win/loss calendar heatmap on the Analytics
            # page. Days with no closed trades still appear, with
            # zero trades, so the grid has no gaps.
            # =================================================

            daily_totals = {}

            for s in closed_trades:

                day = s["closed_at"].split(" ")[0]

                rr_text = str(s.get("rr", "1:1"))

                try:

                    rr_value = float(rr_text.split(":")[1])

                except (ValueError, IndexError):

                    rr_value = 1.0

                if day not in daily_totals:

                    daily_totals[day] = {"net_rr": 0.0, "trades": 0}

                daily_totals[day]["trades"] += 1

                if s.get("result") == "WIN":

                    daily_totals[day]["net_rr"] += rr_value

                else:

                    daily_totals[day]["net_rr"] -= 1.0

            today_date = datetime.now().date()

            heatmap = []

            for i in range(34, -1, -1):

                day = (today_date - timedelta(days=i)).strftime("%Y-%m-%d")

                day_data = daily_totals.get(day, {"net_rr": 0.0, "trades": 0})

                heatmap.append({
                    "date": day,
                    "net_rr": round(day_data["net_rr"], 2),
                    "trades": day_data["trades"]
                })

            stats["daily_heatmap"] = heatmap

            db["stats"] = stats

            return db

        except Exception as e:

            logging.error(f"Database load error: {e}")

    # =========================================================
    # EMPTY DATABASE — returned on first run or corrupt file
    # =========================================================

    return {

        "active": [],
        "history": [],
        "daily_ranges": {},
        "settings": {"telegram_alerts": True},
        "last_scan": None,
        "push_subscriptions": [],

        "stats": {

            "wins": 0,
            "losses": 0,
            "total": 0,

            "rr_won": 0.0,
            "rr_lost": 0.0,
            "net_rr": 0.0,

            "profit_factor": 0.0,
            "expectancy": 0.0,

            "sharpe": None,
            "sortino": None,
            "sample_size": 0,

            "pairs": {},
            "strategies": {},
            "equity_curve": [],
            "daily_heatmap": []
        }
    }

# =========================================================
# SAVE DATABASE
# =========================================================

def save_data(data):

    os.makedirs(DATA_DIR, exist_ok=True)

    to_save = {

        "active": data.get("active", []),

        "history": data.get("history", []),

        "daily_ranges": engine.daily_ranges,

        "settings": data.get("settings", {"telegram_alerts": True}),

        "last_scan": data.get("last_scan"),

        "push_subscriptions": data.get("push_subscriptions", [])
    }

    with open(DB_FILE, "w") as f:

        json.dump(to_save, f, indent=2)

# =========================================================
# ACTIVE TRADE MONITOR
# Checks every active trade against the latest candle and
# closes it as WIN or LOSS if TP/SL was hit.
#
# CREDIT-SAVING FIX: this used to call engine.get_data() once
# PER TRADE, even when several trades shared the same symbol —
# e.g. 8 open ETH/USD trades meant 8 identical API calls in a
# single scan. It now fetches each unique symbol's latest candle
# ONCE and reuses it for every trade on that symbol.
# =========================================================

def evaluate_active_trades(db):

    still_active = []

    active_trades = db.get("active", [])

    symbols_needed = {
        t.get("symbol") for t in active_trades if t.get("symbol")
    }

    # One API call per unique symbol, not per trade.
    symbol_data = {}

    for symbol in symbols_needed:

        try:

            symbol_data[symbol] = engine.get_data(
                symbol,
                "5min",
                outputsize=2
            )

        except Exception as e:

            logging.error(
                f"Error fetching data for active-trade check [{symbol}]: {e}"
            )

            symbol_data[symbol] = None

    for trade in active_trades:

        symbol = trade.get("symbol")

        try:

            df = symbol_data.get(symbol)

            if df is None or df.empty:

                still_active.append(trade)

                continue

            last_candle = df.iloc[-1]

            high = float(last_candle["high"])

            low = float(last_candle["low"])

            tp = float(trade.get("tp", 0))

            sl = float(trade.get("sl", 0))

            trade_type = trade.get("type")

            was_hit = False

            result = None

            # =============================================
            # BUY TRADE EVALUATION
            # =============================================

            if trade_type == "BUY":

                if high >= tp:

                    was_hit = True

                    result = "WIN"

                elif low <= sl:

                    was_hit = True

                    result = "LOSS"

            # =============================================
            # SELL TRADE EVALUATION
            # =============================================

            elif trade_type == "SELL":

                if low <= tp:

                    was_hit = True

                    result = "WIN"

                elif high >= sl:

                    was_hit = True

                    result = "LOSS"

            # =============================================
            # CLOSE TRADE
            # =============================================

            if was_hit:

                trade["result"] = result

                trade["closed_at"] = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                db["history"].insert(0, trade)

                logging.info(
                    f"Trade Closed: {symbol} | {trade_type} | {result}"
                )

            else:

                still_active.append(trade)

        except Exception as e:

            logging.error(
                f"Error evaluating active trade [{symbol}]: {e}"
            )

            still_active.append(trade)

    db["active"] = still_active

    return db

# =========================================================
# SCAN AND UPDATE
# =========================================================

def _signal_key(s):
    return (
        s.get("symbol"),
        s.get("type"),
        s.get("entry"),
        s.get("time")
    )


def scan_and_update(db):

    try:
        signals = engine.check_strategies(engine.daily_ranges)
    except Exception as e:
        logging.error(f"Strategy scan error: {e}")
        return db

    telegram_enabled = db.get("settings", {}).get("telegram_alerts", True)

    existing_keys = set()

    for t in db.get("active", []):
        existing_keys.add(_signal_key(t))

    for t in db.get("history", []):
        existing_keys.add(_signal_key(t))

    for signal in signals:

        key = _signal_key(signal)

        if key not in existing_keys:

            db["active"].append(signal)
            existing_keys.add(key)

            logging.info(
                f"New Signal: {signal['symbol']} | "
                f"{signal['type']} | {signal['strat']}"
            )

            if telegram_enabled:

                dual_time = format_dual_time(signal.get("time", ""))

                send_telegram_alert(
                    f"New {signal['type']} signal — {signal['symbol']}\n"
                    f"Entry: {signal['entry']}\n"
                    f"SL: {signal['sl']}\n"
                    f"TP: {signal['tp']}\n"
                    f"RR: {signal['rr']}\n"
                    f"Strategy: {signal['strat']}\n"
                    f"Candle: {dual_time['ny']} / {dual_time['wat']}"
                )

            # Push notifications go to whichever devices are
            # currently subscribed — there's no separate on/off
            # setting for this, since subscribing IS the opt-in
            # (per device, via the Settings toggle).
            if db.get("push_subscriptions"):

                send_push_to_all(
                    db,
                    title=f"{signal['type']} Signal — {signal['symbol']}",
                    body=(
                        f"Entry {signal['entry']} | SL {signal['sl']} | "
                        f"TP {signal['tp']} | RR {signal['rr']}"
                    )
                )

    return db

# =========================================================
# SCHEDULED SCAN
# Runs every 5 minutes as before, but before 8:00 AM NY time it
# does nothing except log that it's skipping — no API calls, no
# scan, no evaluation. Full scanning resumes automatically once
# NY hits 8 AM.
# =========================================================

def scheduled_scan():

    with data_lock:

        try:

            if not is_within_active_window():

                logging.info(
                    "Outside active scanning window (before "
                    f"{ACTIVE_WINDOW_START_HOUR}:00 AM NY) — skipping "
                    "this scan to conserve API credits."
                )

                return

            db = load_data()

            db = evaluate_active_trades(db)

            db = scan_and_update(db)

            db["last_scan"] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            save_data(db)

            logging.info("Scheduled scan complete.")

        except Exception as e:

            logging.error(f"Scheduled scan error: {e}")


scheduler = BackgroundScheduler()

scheduler.add_job(
    scheduled_scan,
    "interval",
    minutes=SCAN_INTERVAL_MINUTES,
    id="market_scan",
    next_run_time=datetime.now()
)


@app.on_event("startup")
def start_scheduler():
    logging.info(
        f"Twelve Data API keys detected at startup: {len(engine.keys)}"
    )
    scheduler.start()


@app.on_event("shutdown")
def stop_scheduler():
    scheduler.shutdown(wait=False)

# =========================================================
# SETTINGS — TELEGRAM TOGGLE
# =========================================================

@app.post("/settings/toggle-telegram")
def toggle_telegram_alerts():

    with data_lock:

        db = load_data()

        current = db.get("settings", {}).get("telegram_alerts", True)

        new_value = not current

        db.setdefault("settings", {})["telegram_alerts"] = new_value

        save_data(db)

    return JSONResponse({"telegram_alerts": new_value})

# =========================================================
# PUSH NOTIFICATIONS — subscribe / unsubscribe / service worker
# =========================================================

@app.get("/push/vapid-public-key")
def get_vapid_public_key():
    return JSONResponse({"publicKey": VAPID_PUBLIC_KEY})


@app.post("/push/subscribe")
async def push_subscribe(request: Request):

    body = await request.json()

    endpoint = body.get("endpoint")

    if not endpoint:
        return JSONResponse({"status": "error", "reason": "missing endpoint"}, status_code=400)

    with data_lock:

        db = load_data()

        subs = db.setdefault("push_subscriptions", [])

        # Dedupe by endpoint — resubscribing the same device
        # replaces its old record instead of piling up duplicates.
        subs[:] = [s for s in subs if s.get("endpoint") != endpoint]

        subs.append(body)

        save_data(db)

    return JSONResponse({"status": "subscribed"})


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):

    body = await request.json()

    endpoint = body.get("endpoint")

    with data_lock:

        db = load_data()

        subs = db.get("push_subscriptions", [])

        subs[:] = [s for s in subs if s.get("endpoint") != endpoint]

        save_data(db)

    return JSONResponse({"status": "unsubscribed"})


@app.get("/service-worker.js")
def service_worker():

    try:

        with open("service-worker.js", "r") as f:
            content = f.read()

        return Response(content=content, media_type="application/javascript")

    except FileNotFoundError:

        return Response(content="", media_type="application/javascript", status_code=404)

# =========================================================
# CHART DATA
# Serves cached candles + the bot's own range/trade data for the
# in-app chart. Reads only from what the scheduler already fetched
# during its normal scans — never triggers a new Twelve Data call.
# =========================================================

@app.get("/chart-data/{symbol:path}")
def chart_data(symbol: str):

    with data_lock:
        db = load_data()

    candles_raw = engine.latest_candles.get(symbol, [])

    candles = []

    for c in candles_raw:

        ts = ny_str_to_unix(str(c.get("datetime", "")))

        if ts is None:
            continue

        candles.append({
            "time": ts,
            "open": float(c.get("open", 0)),
            "high": float(c.get("high", 0)),
            "low": float(c.get("low", 0)),
            "close": float(c.get("close", 0))
        })

    range_info = engine.daily_ranges.get(symbol, {})

    trades = []

    for t in db.get("active", []) + db.get("history", []):

        if t.get("symbol") != symbol:
            continue

        ts = ny_str_to_unix(str(t.get("time", "")))

        trades.append({
            "type": t.get("type"),
            "entry": t.get("entry"),
            "sl": t.get("sl"),
            "tp": t.get("tp"),
            "time": ts,
            "result": t.get("result", "ACTIVE")
        })

    return JSONResponse({
        "symbol": symbol,
        "candles": candles,
        "range": {
            "high": range_info.get("high"),
            "low": range_info.get("low")
        },
        "trades": trades
    })

# =========================================================
# DASHBOARD
# =========================================================

@app.get("/", response_class=HTMLResponse)
def dashboard():

    with data_lock:
        db = load_data()

    try:

        with open("index.html", "r") as f:

            template = f.read()

    except FileNotFoundError:

        return HTMLResponse(
            content="<h2>index.html not found</h2>",
            status_code=500
        )

    stats = db["stats"]

    # =====================================================
    # HISTORY CARDS (tap-to-expand, filterable, sortable)
    # =====================================================

    history_rows = ""

    for s in db.get("history", [])[:40]:

        symbol = s.get("symbol", "-")
        trade_type = s.get("type", "-")
        result = s.get("result", "-")
        entry = s.get("entry", "-")

        type_color = "#00ff99" if trade_type == "BUY" else "#ff3b6b"
        result_color = "#00ff99" if result == "WIN" else "#ff3b6b"

        signal_time = format_dual_time(s.get("time", ""))
        closed_time = format_dual_time(s.get("closed_at", "")) if s.get("closed_at") else None

        sig_id = f"{symbol}|{trade_type}|{entry}|{s.get('time', '')}"

        closed_rows_html = ""
        if closed_time:
            closed_rows_html = (
                f"<div class='tc-row'><span>Closed (NY)</span><span>{closed_time['ny']}</span></div>"
                f"<div class='tc-row'><span>Closed (WAT)</span><span>{closed_time['wat']}</span></div>"
            )

        history_rows += (
            f"<div class='trade-card' data-sig=\"{sig_id}\" "
            f"data-type='{trade_type}' data-result='{result}'>"
            f"<div class='trade-card-header' onclick='toggleCard(this)'>"
            f"<span class='tc-symbol'>{symbol}</span>"
            f"<span class='tc-type' style='color:{type_color}'>{trade_type}</span>"
            f"<span class='tc-entry'>{entry}</span>"
            f"<span class='tc-status' style='color:{result_color}'>{result}</span>"
            f"<span class='tc-chevron'>&#9662;</span>"
            f"</div>"
            f"<div class='trade-card-detail'>"
            f"<div class='tc-row'><span>Stop Loss</span><span>{s.get('sl', '-')}</span></div>"
            f"<div class='tc-row'><span>Take Profit</span><span>{s.get('tp', '-')}</span></div>"
            f"<div class='tc-row'><span>Risk:Reward</span><span>{s.get('rr', '-')}</span></div>"
            f"<div class='tc-row'><span>Signal (NY)</span><span>{signal_time['ny']}</span></div>"
            f"<div class='tc-row'><span>Signal (WAT)</span><span>{signal_time['wat']}</span></div>"
            f"{closed_rows_html}"
            f"</div>"
            f"</div>"
        )

    if not history_rows:
        history_rows = '<div class="empty-state">No history yet</div>'

    # =====================================================
    # ACTIVE SIGNAL CARDS (tap-to-expand)
    # =====================================================

    active_rows = ""

    for s in db.get("active", []):

        symbol = s.get("symbol", "-")
        trade_type = s.get("type", "-")
        entry = s.get("entry", "-")

        type_color = "#00ff99" if trade_type == "BUY" else "#ff3b6b"

        signal_time = format_dual_time(s.get("time", ""))

        sig_id = f"{symbol}|{trade_type}|{entry}|{s.get('time', '')}"

        active_rows += (
            f"<div class='trade-card' data-sig=\"{sig_id}\" "
            f"data-type='{trade_type}' data-result='ACTIVE'>"
            f"<div class='trade-card-header' onclick='toggleCard(this)'>"
            f"<span class='tc-symbol'>{symbol}</span>"
            f"<span class='tc-type' style='color:{type_color}'>{trade_type}</span>"
            f"<span class='tc-entry'>{entry}</span>"
            f"<span class='tc-status' style='color:#38bdf8'>ACTIVE</span>"
            f"<span class='tc-chevron'>&#9662;</span>"
            f"</div>"
            f"<div class='trade-card-detail'>"
            f"<div class='tc-row'><span>Stop Loss</span><span>{s.get('sl', '-')}</span></div>"
            f"<div class='tc-row'><span>Take Profit</span><span>{s.get('tp', '-')}</span></div>"
            f"<div class='tc-row'><span>Risk:Reward</span><span>{s.get('rr', '-')}</span></div>"
            f"<div class='tc-row'><span>Signal (NY)</span><span>{signal_time['ny']}</span></div>"
            f"<div class='tc-row'><span>Signal (WAT)</span><span>{signal_time['wat']}</span></div>"
            f"</div>"
            f"</div>"
        )

    if not active_rows:
        active_rows = '<div class="empty-state">No active signals</div>'

    # =====================================================
    # PAIR STATS CARDS
    # =====================================================

    pair_html = ""

    for pair, p_data in stats["pairs"].items():

        wr = (
            (p_data["wins"] / p_data["total"]) * 100
            if p_data["total"] > 0
            else 0
        )

        pair_html += (
            f"<div class='pair-card'>"
            f"<div class='p-name'>{pair}</div>"
            f"<div class='p-wr'>{wr:.1f}%</div>"
            f"<div class='p-count'>{p_data['total']} Signals</div>"
            f"<div class='p-count'>RR: {round(p_data['rr'], 2)}</div>"
            f"</div>"
        )

    if not pair_html:
        pair_html = (
            '<div class="pair-card">'
            '<div class="p-name">No Data</div>'
            '<div class="p-wr">—</div>'
            '<div class="p-count">0 Signals</div>'
            '</div>'
        )

    # =====================================================
    # STRATEGY STATS ROWS
    # =====================================================

    strategy_html = ""

    for strat_name, s_data in stats["strategies"].items():

        s_wr = (
            (s_data["wins"] / s_data["total"]) * 100
            if s_data["total"] > 0
            else 0
        )

        strategy_html += (
            f"<tr>"
            f"<td>{strat_name}</td>"
            f"<td>{s_data['total']}</td>"
            f"<td style='color:#00ff99'>{s_data['wins']}</td>"
            f"<td style='color:#ff3b6b'>{s_data['losses']}</td>"
            f"<td>{s_wr:.1f}%</td>"
            f"</tr>"
        )

    if not strategy_html:
        strategy_html = (
            '<tr><td colspan="5" class="empty-state">'
            'No strategy data yet</td></tr>'
        )

    # =====================================================
    # GLOBAL WIN RATE
    # =====================================================

    global_wr = (
        (stats["wins"] / stats["total"]) * 100
        if stats["total"] > 0
        else 0
    )

    last_scan = db.get("last_scan") or "Never (waiting on first scheduled scan)"

    # =====================================================
    # SETTINGS — TELEGRAM TOGGLE STATE
    # =====================================================

    telegram_enabled = db.get("settings", {}).get("telegram_alerts", True)

    telegram_checked = "checked" if telegram_enabled else ""

    # =====================================================
    # EQUITY CURVE DATA
    # =====================================================

    equity_data_json = json.dumps(stats.get("equity_curve", []))

    heatmap_data_json = json.dumps(stats.get("daily_heatmap", []))

    symbols_json = json.dumps(engine.RANGE_PAIRS)

    vapid_public_key_json = json.dumps(VAPID_PUBLIC_KEY)

    # =====================================================
    # SHARPE / SORTINO DISPLAY
    # =====================================================

    sharpe_display = (
        stats["sharpe"] if stats.get("sharpe") is not None else "N/A"
    )

    sortino_display = (
        stats["sortino"] if stats.get("sortino") is not None else "N/A"
    )

    # =====================================================
    # TEMPLATE VARIABLE INJECTION
    # =====================================================

    html = template.replace("{{TOTAL}}", str(stats["total"]))
    html = html.replace("{{WINRATE}}", f"{global_wr:.1f}%")
    html = html.replace("{{LAST_SCAN}}", last_scan)
    html = html.replace("{{ACTIVE_SIGNALS}}", active_rows)
    html = html.replace("{{TOTAL_WINS}}", str(stats["wins"]))
    html = html.replace("{{TOTAL_LOSSES}}", str(stats["losses"]))
    html = html.replace("{{RR_WON}}", str(stats["rr_won"]))
    html = html.replace("{{RR_LOST}}", str(stats["rr_lost"]))
    html = html.replace("{{NET_RR}}", str(stats["net_rr"]))
    html = html.replace("{{PROFIT_FACTOR}}", str(stats["profit_factor"]))
    html = html.replace("{{EXPECTANCY}}", str(stats["expectancy"]))
    html = html.replace("{{SHARPE}}", str(sharpe_display))
    html = html.replace("{{SORTINO}}", str(sortino_display))
    html = html.replace("{{PAIR_STATS}}", pair_html)
    html = html.replace("{{STRATEGY_ROWS}}", strategy_html)
    html = html.replace("{{SIGNALS}}", history_rows)
    html = html.replace("{{SCAN_INTERVAL}}", f"{SCAN_INTERVAL_MINUTES} Minutes")
    html = html.replace("{{TELEGRAM_CHECKED}}", telegram_checked)
    html = html.replace("{{EQUITY_DATA}}", equity_data_json)
    html = html.replace("{{HEATMAP_DATA}}", heatmap_data_json)
    html = html.replace("{{SYMBOLS}}", symbols_json)
    html = html.replace("{{VAPID_PUBLIC_KEY}}", vapid_public_key_json)

    return HTMLResponse(content=html)

# =========================================================
# SERVER ENTRY POINT
# =========================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
