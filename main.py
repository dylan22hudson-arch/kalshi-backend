from flask import Flask, jsonify, send_file
from flask_cors import CORS
import requests, os, re, time, threading, logging
from datetime import datetime, date

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

KALSHI_KEY    = os.environ.get("KALSHI_API_KEY")
BASE_URL      = "https://api.elections.kalshi.com/trade-api/v2"

MAX_BET        = float(os.environ.get("MAX_BET", 25))
DAILY_LIMIT    = float(os.environ.get("DAILY_LIMIT", 100))
MIN_EDGE       = float(os.environ.get("MIN_EDGE", 0.10))
KELLY_FRAC     = float(os.environ.get("KELLY_FRAC", 0.25))
BANKROLL       = float(os.environ.get("BANKROLL", 500))
AUTO_ENABLED   = os.environ.get("AUTO_ENABLED", "false").lower() == "true"
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL", 300))
DAILY_TARGET   = float(os.environ.get("DAILY_TARGET", 25))
MAX_HOURS      = int(os.environ.get("MAX_HOURS", 24))

daily_state = {
    "date":        str(date.today()),
    "spent":       0.0,
    "profit":      0.0,
    "target_hit":  False,
    "trade_count": 0
}
trade_log = []

BASE_RATES = {
    "spx_up":    {"pattern": r"s.?p.*(above|over|higher|up|close above|end above)",   "rate": 0.52, "source": "S&P daily 2000-2024"},
    "spx_down":  {"pattern": r"s.?p.*(below|under|lower|down|close below|end below)", "rate": 0.48, "source": "S&P daily 2000-2024"},
    "btc_up":    {"pattern": r"bitcoin|btc.*(above|over|higher|up)",                  "rate": 0.52, "source": "BTC daily 2018-2024"},
    "btc_down":  {"pattern": r"bitcoin|btc.*(below|under|lower|down)",                "rate": 0.48, "source": "BTC daily 2018-2024"},
    "nasdaq_up": {"pattern": r"nasdaq|qqq.*(above|over|higher|up)",                   "rate": 0.53, "source": "Nasdaq daily 2000-2024"},
    "oil_up":    {"pattern": r"(oil|crude|wti).*(above|over|up|higher)",              "rate": 0.51, "source": "WTI daily 2000-2024"},
    "rain_nyc":  {"pattern": r"rain.*(new york|nyc|ny)|new york.*rain",               "rate": 0.28, "source": "NOAA NYC 2000-2024"},
    "rain_la":   {"pattern": r"rain.*(los angeles|la )|los angeles.*rain",            "rate": 0.12, "source": "NOAA LA 2000-2024"},
    "snow":      {"pattern": r"snow.*(inch|cm|accumul)",                              "rate": 0.18, "source": "NOAA snowfall"},
    "temp_hot":  {"pattern": r"temperature.*(above|exceed|over)\s*\d+",              "rate": 0.45, "source": "NOAA temp data"},
    "nba_fav":   {"pattern": r"nba.*(win|beat|defeat)|will the .* win",              "rate": 0.62, "source": "NBA win rate 2000-2024"},
    "nfl_fav":   {"pattern": r"nfl.*(win|beat|defeat)",                              "rate": 0.58, "source": "NFL win rate 2000-2024"},
    "mlb_fav":   {"pattern": r"mlb.*(win|beat|defeat)",                              "rate": 0.55, "source": "MLB win rate 2000-2024"},
    "nhl_fav":   {"pattern": r"nhl.*(win|beat|defeat)",                              "rate": 0.57, "source": "NHL win rate 2000-2024"},
    "jobless":   {"pattern": r"jobless.*(below|above|exceed|under)",                 "rate": 0.45, "source": "DOL claims 2000-2024"},
    "fed_hold":  {"pattern": r"fed.*(hold|pause|unchanged|no change)",               "rate": 0.70, "source": "FRED meetings 2000-2024"},
    "fed_cut":   {"pattern": r"fed.*(cut|lower|reduce|pivot)",                       "rate": 0.30, "source": "FRED 2000-2024"},
    "recession": {"pattern": r"recession",                                            "rate": 0.15, "source": "NBER 1945-2024"},
    "shutdown":  {"pattern": r"government.*(shutdown|close)",                         "rate": 0.20, "source": "CRS history"},
    "nohitter":  {"pattern": r"no.hitter",                                            "rate": 0.25, "source": "Baseball Reference"},
    "hurricane": {"pattern": r"hurricane.*(gulf|florida|texas|landfall)",             "rate": 0.35, "source": "NOAA 1950-2024"},
}

def get_headers():
    return {"Authorization": f"Bearer {KALSHI_KEY}", "Content-Type": "application/json"}

def get_base_rate(title):
    t = (title or "").lower()
    for key, data in BASE_RATES.items():
        if re.search(data["pattern"], t):
            return {"rate": data["rate"], "source": data["source"], "matched": key}
    return None

def is_short_dated(market):
    close_ts = market.get("close_time") or market.get("expiration_time")
    if not close_ts:
        return False
    try:
        from datetime import timezone
        if isinstance(close_ts, str):
            close_ts = close_ts.replace("Z", "+00:00")
            close_dt = datetime.fromisoformat(close_ts)
        else:
            close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        hours_left = (close_dt - now).total_seconds() / 3600
        return 0 < hours_left <= MAX_HOURS
    except:
        return False

def get_hours_left(market):
    try:
        from datetime import timezone
        close_ts = market.get("close_time") or market.get("expiration_time")
        if not close_ts:
            return None
        if isinstance(close_ts, str):
            close_ts = close_ts.replace("Z", "+00:00")
            close_dt = datetime.fromisoformat(close_ts)
        else:
            close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        return round((close_dt - now).total_seconds() / 3600, 1)
    except:
        return None

def kelly(p_true, p_market, side):
    if side == "NO":
        p_true   = 1 - p_true
        p_market = 1 - p_market
    b = (1 - p_market) / p_market
    k = (b * p_true - (1 - p_true)) / b
    return max(0.0, k)

def compute_edge(market_price, base_rate):
    diff = market_price - base_rate
    if diff > MIN_EDGE:
        return {"signal": "FADE", "direction": "NO",      "magnitude": round(abs(diff) * 100)}
    elif diff < -MIN_EDGE:
        return {"signal": "BET",  "direction": "YES",     "magnitude": round(abs(diff) * 100)}
    return    {"signal": "SKIP",  "direction": "NEUTRAL", "magnitude": round(abs(diff) * 100)}

def estimate_profit(bet_amt, market_price, direction):
    if direction == "YES":
        return round(bet_amt * (1 - market_price) / market_price, 2)
    else:
        return round(bet_amt * market_price / (1 - market_price), 2)

def reset_daily_if_needed():
    today = str(date.today())
    if daily_state["date"] != today:
        daily_state.update({
            "date":        today,
            "spent":       0.0,
            "profit":      0.0,
            "target_hit":  False,
            "trade_count": 0
        })

def place_order(ticker, side, price_cents, quantity):
    payload = {
        "ticker":          ticker,
        "side":            side.lower(),
        "type":            "limit",
        "yes_price":       price_cents,
        "count":           quantity,
        "action":          "buy",
        "client_order_id": f"autobot-{ticker}-{int(time.time())}"
    }
    r = requests.post(f"{BASE_URL}/portfolio/orders", json=payload, headers=get_headers())
    return r.json()

def scan_and_trade():
    while True:
        try:
            logging.info("── Scanning 24h markets ──")
            reset_daily_if_needed()

            r = requests.get(
                f"{BASE_URL}/markets?limit=200&status=open",
                headers=get_headers()
            )
            markets = r.json().get("markets", [])
            short_markets = [m for m in markets if is_short_dated(m)]
            logging.info(f"Found {len(short_markets)} markets closing within {MAX_HOURS}h")

            for m in short_markets:
                ticker = m.get("ticker", "")
                title  = m.get("title",  "")
                bid    = m.get("yes_bid", 0) or 0
                ask    = m.get("yes_ask", 0) or 0
                mid    = ((bid + ask) / 2) / 100 if bid and ask else (bid or ask) / 100
                if mid <= 0:
                    continue

                br = get_base_rate(title)
                if not br:
                    continue

                edge = compute_edge(mid, br["rate"])
                if edge["signal"] == "SKIP":
                    continue

                k      = kelly(br["rate"], mid, edge["direction"])
                k_adj  = k * KELLY_FRAC
                bet_$  = min(BANKROLL * k_adj, MAX_BET)
                bet_$  = round(bet_$, 2)

                if bet_$ < 1.0:
                    continue

                if daily_state["spent"] + bet_$ > DAILY_LIMIT:
                    logging.info(f"Daily spend limit hit — skipping {ticker}")
                    break

                if any(t["ticker"] == ticker and t["date"] == str(date.today()) for t in trade_log):
                    continue

                est_profit = estimate_profit(bet_$, mid, edge["direction"])

                log_entry = {
                    "date":        str(date.today()),
                    "time":        datetime.now().strftime("%H:%M:%S"),
                    "ticker":      ticker,
                    "title":       title[:60],
                    "signal":      edge["signal"],
                    "direction":   edge["direction"],
                    "edge_pct":    edge["magnitude"],
                    "crowd":       round(mid * 100),
                    "base_rate":   round(br["rate"] * 100),
                    "bet_$":       bet_$,
                    "est_profit":  est_profit,
                    "hours_left":  get_hours_left(m),
                    "status":      "PAPER" if not AUTO_ENABLED else "LIVE",
                    "result":      None
                }

                if AUTO_ENABLED:
                    price_cents = round(mid * 100) if edge["direction"] == "YES" else round((1 - mid) * 100)
                    quantity    = max(1, int(bet_$ / (price_cents / 100)))
                    order       = place_order(ticker, edge["direction"], price_cents, quantity)
                    log_entry["result"] = order
                    daily_state["spent"]       += bet_$
                    daily_state["profit"]      += est_profit
                    daily_state["trade_count"] += 1
                    daily_state["target_hit"]   = daily_state["profit"] >= DAILY_TARGET
                    logging.info(f"PLACED {edge['direction']} {ticker} ${bet_$:.2f} est_profit=${est_profit:.2f}")
                else:
                    logging.info(f"PAPER {edge['direction']} {ticker} ${bet_$:.2f} est_profit=${est_profit:.2f}")
                    daily_state["trade_count"] += 1

                trade_log.append(log_entry)
                if len(trade_log) > 500:
                    trade_log.pop(0)

        except Exception as e:
            logging.error(f"Scan error: {e}")

        time.sleep(SCAN_INTERVAL)

@app.route("/")
def index():
    return send_file("dashboard.html")

@app.route("/markets")
def markets():
    r = requests.get(
        f"{BASE_URL}/markets?limit=200&status=open",
        headers=get_headers()
    )
    data = r.json()
    result = []
    for m in data.get("markets", []):
        bid  = m.get("yes_bid", 0) or 0
        ask  = m.get("yes_ask", 0) or 0
        mid  = ((bid + ask) / 2) / 100 if bid and ask else (bid or ask) / 100
        m["mid_price"]   = round(mid, 4)
        m["short_dated"] = is_short_dated(m)
        m["hours_left"]  = get_hours_left(m)
        br = get_base_rate(m.get("title", ""))
        if br:
            m["base_rate"] = br
            m["edge"]      = compute_edge(mid, br["rate"])
        else:
            m["base_rate"] = None
            m["edge"]      = None
        result.append(m)
    return jsonify({"markets": result})

@app.route("/trades")
def trades():
    return jsonify({
        "trades":       list(reversed(trade_log)),
        "daily_state":  daily_state,
        "daily_target": DAILY_TARGET,
        "auto_enabled": AUTO_ENABLED,
        "limits": {
            "max_bet":     MAX_BET,
            "daily_limit": DAILY_LIMIT,
            "min_edge":    MIN_EDGE,
            "max_hours":   MAX_HOURS
        }
    })

@app.route("/status")
def status():
    return jsonify({
        "auto_enabled":    AUTO_ENABLED,
        "daily_spent":     daily_state["spent"],
        "daily_profit":    daily_state["profit"],
        "daily_target":    DAILY_TARGET,
        "target_hit":      daily_state["target_hit"],
        "trade_count":     daily_state["trade_count"],
        "daily_limit":     DAILY_LIMIT,
        "daily_remaining": DAILY_LIMIT - daily_state["spent"],
        "max_bet":         MAX_BET,
        "min_edge":        MIN_EDGE,
        "kelly_fraction":  KELLY_FRAC,
        "bankroll":        BANKROLL,
        "scan_interval":   SCAN_INTERVAL,
        "max_hours":       MAX_HOURS
    })

if __name__ == "__main__":
    t = threading.Thread(target=scan_and_trade, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8080)
