import os
import json
import logging
import threading
import uvicorn
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import engine
from datetime import datetime

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = FastAPI()

DB_FILE = "data.json"

# =========================================================
# SCAN INTERVAL
# The scheduler (not the dashboard route) drives scanning now.
# =========================================================

SCAN_INTERVAL_MINUTES = 5

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

                "pairs": {},
                "strategies": {},
                "equity_curve": []
            }

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

                # -----------------------------------------
                # LOSSES
                # -----------------------------------------

                elif s.get("result") == "LOSS":

                    stats["losses"] += 1

                    stats["rr_lost"] += 1.0

                    stats["pairs"][symbol]["losses"] += 1

                    stats["pairs"][symbol]["rr"] -= 1.0

                    stats["strategies"][strategy]["losses"] += 1

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
            # EQUITY CURVE
            # Cumulative net RR over time, built from closed_at
            # timestamps. History is stored newest-first, so it
            # has to be sorted chronologically before accumulating.
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

        "stats": {

            "wins": 0,
            "losses": 0,
            "total": 0,

            "rr_won": 0.0,
            "rr_lost": 0.0,
            "net_rr": 0.0,

            "profit_factor": 0.0,
            "expectancy": 0.0,

            "pairs": {},
            "strategies": {},
            "equity_curve": []
        }
    }

# =========================================================
# SAVE DATABASE
# Saves active trades, history, daily_ranges, settings, and
# last_scan to disk. The computed stats dict is NOT saved — it
# is always recalculated fresh from history on load.
# =========================================================

def save_data(data):

    to_save = {

        "active": data.get("active", []),

        "history": data.get("history", []),

        # Persist today's range so it survives restarts.
        "daily_ranges": engine.daily_ranges,

        "settings": data.get("settings", {"telegram_alerts": True}),

        "last_scan": data.get("last_scan")
    }

    with open(DB_FILE, "w") as f:

        json.dump(to_save, f, indent=2)

# =========================================================
# ACTIVE TRADE MONITOR
# Checks every active trade against the latest candle
# and closes it as WIN or LOSS if TP/SL was hit.
# =========================================================

def evaluate_active_trades(db):

    still_active = []

    for trade in db.get("active", []):

        symbol = trade.get("symbol")

        # Only the Hybrid Fake Breakout strategy remains, and it
        # is evaluated on the 5-minute timeframe.
        interval = "5min"

        try:

            df = engine.get_data(
                symbol,
                interval,
                outputsize=2
            )

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
# Runs the strategy engine, adds any genuinely new signals to
# active trades, sends a Telegram alert for each (if enabled),
# and saves everything.
#
# Since engine.check_strategies() now re-derives the whole day
# from scratch every scan, the SAME already-recorded signal can
# come back on every pass. Duplicate-checking has to compare
# against both active trades AND history (not just active), or
# an already-closed trade could get resurrected as "new".
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

                send_telegram_alert(
                    f"New {signal['type']} signal — {signal['symbol']}\n"
                    f"Entry: {signal['entry']}\n"
                    f"SL: {signal['sl']}\n"
                    f"TP: {signal['tp']}\n"
                    f"RR: {signal['rr']}\n"
                    f"Strategy: {signal['strat']}"
                )

    return db

# =========================================================
# SCHEDULED SCAN
# Runs on its own timer, independent of anyone loading the
# dashboard.
# =========================================================

def scheduled_scan():

    with data_lock:

        try:

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
# DASHBOARD
# Read-only render. Scanning and evaluation are handled entirely
# by the background scheduler now — this route just displays
# whatever the latest saved state is.
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
    # HISTORY TABLE ROWS
    # =====================================================

    history_rows = ""

    for s in db.get("history", [])[:20]:

        type_color = (
            "#00ff99"
            if s.get("type") == "BUY"
            else "#ff3b6b"
        )

        result_color = (
            "#00ff99"
            if s.get("result") == "WIN"
            else "#ff3b6b"
        )

        history_rows += (
            f"<tr>"
            f"<td>{s.get('symbol', '-')}</td>"
            f"<td style='color:{type_color};font-weight:700'>"
            f"{s.get('type', '-')}</td>"
            f"<td>{s.get('entry', '-')}</td>"
            f"<td>{s.get('sl', '-')}</td>"
            f"<td>{s.get('tp', '-')}</td>"
            f"<td>{s.get('rr', '-')}</td>"
            f"<td style='color:{result_color};font-weight:700'>"
            f"{s.get('result', '-')}</td>"
            f"</tr>"
        )

    if not history_rows:
        history_rows = (
            '<tr><td colspan="7" class="empty-state">'
            'No history yet</td></tr>'
        )

    # =====================================================
    # ACTIVE TABLE ROWS
    # =====================================================

    active_rows = ""

    for s in db.get("active", []):

        type_color = (
            "#00ff99"
            if s.get("type") == "BUY"
            else "#ff3b6b"
        )

        active_rows += (
            f"<tr>"
            f"<td>{s.get('symbol', '-')}</td>"
            f"<td style='color:{type_color};font-weight:700'>"
            f"{s.get('type', '-')}</td>"
            f"<td>{s.get('entry', '-')}</td>"
            f"<td>{s.get('sl', '-')}</td>"
            f"<td>{s.get('tp', '-')}</td>"
            f"<td>{s.get('rr', '-')}</td>"
            f"<td style='color:#38bdf8;font-weight:700'>"
            f"ACTIVE</td>"
            f"</tr>"
        )

    if not active_rows:
        active_rows = (
            '<tr><td colspan="7" class="empty-state">'
            'No active signals</td></tr>'
        )

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
    html = html.replace("{{PAIR_STATS}}", pair_html)
    html = html.replace("{{STRATEGY_ROWS}}", strategy_html)
    html = html.replace("{{SIGNALS}}", history_rows)
    html = html.replace("{{SCAN_INTERVAL}}", f"{SCAN_INTERVAL_MINUTES} Minutes")
    html = html.replace("{{TELEGRAM_CHECKED}}", telegram_checked)
    html = html.replace("{{EQUITY_DATA}}", equity_data_json)

    return HTMLResponse(content=html)

# =========================================================
# SERVER ENTRY POINT
# =========================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
