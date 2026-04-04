from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, os, re, time, threading, logging
from datetime import datetime, date

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

KALSHI_KEY    = os.environ.get("KALSHI_API_KEY")
BASE_URL      = "https://api.elections.kalshi.com/trade-api/v2"

# ── Safety controls ──────────────────────────────────────────────
MAX_BET       = float(os.environ.get("MAX_BET", 25))        # max $ per bet
DAILY_LIMIT   = float(os.environ.get("DAILY_LIMIT", 100))   # max $ per day
MIN_EDGE      = float(os.environ.get("MIN_EDGE", 0.10))      # min edge to bet
KELLY_FRAC    = float(os.environ.get("KELLY_FRAC", 0.25))    # quarter Kelly = safer
BANKROLL      = float(os.environ.get("BANKROLL", 500))       # your total budget
AUTO_ENABLED  = os.environ.get("AUTO_ENABLED", "false").lower() == "true"
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", 300))    # seconds between scans

# ── State ────────────────────────────────────────────────────────
daily_spent   = {"date": str(date.today()), "amount": 0.0}
trade_log     = []

BASE_RATES = {
    "fed_cut":      {"pattern": r"fed.*(cut|lower|reduce|pivot)", "rate": 0.30, "source": "FRED 2000-2024"},
    "fed_hike":     {"pattern": r"fed.*(hike|raise|increase)",    "rate": 0.25, "source": "FRED 2000-2024"},
    "cpi_high":     {"pattern": r"cpi.*(above|exceed|over)\s*[45]","rate": 0.18,"source": "BLS 2000-2024"},
    "recession":    {"pattern": r"recession",                      "rate": 0.15, "source": "NBER 1945-2024"},
    "shutdown":     {"pattern": r"government.*(shutdown|close)",   "rate": 0.20, "source": "CRS history"},
    "nohitter":     {"pattern": r"no.hitter",                      "rate": 0.25, "source": "Baseball Reference"},
    "hurricane":    {"pattern": r"hurricane.*(gulf|florida|texas|landfall)", "rate": 0.35, "source": "NOAA 1950-2024"},
    "snowfall":     {"pattern": r"snow.*(inch|feet|cm)",           "rate": 0.22, "source": "NOAA climate"},
    "billion_deal": {"pattern": r"acqui|merger|deal.*(billion)",   "rate": 0.12, "source": "M&A base rates"},
}

def headers():
    return {"Authorization": f"Bearer {KALSHI_KEY}", "Content-Type": "application/json"}

def get_base_rate(title):
    t = (title or "").lower()
    for key, data in BASE_RATES.items():
        if re.search(data["pattern"], t):
            return {"rate": data["rate"], "source": data["source"], "matched": key}
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

def reset_daily_if_needed():
    today = str(date.today())
    if daily_spent["date"] != today:
        daily_spent["date"]   = today
        daily_spent["amount"] = 0.0

def place_order(ticker, side, price_cents, quantity):
    """Place a real limit order on Kalshi."""
    payload = {
        "ticker":          ticker,
        "side":            side.lower(),
        "type":            "limit",
        "yes_price":       price_cents,
        "count":           quantity,
        "action":          "buy",
        "client_order_id": f"autobot-{ticker}-{int(time.time())}"
    }
    r = requests.post(f"{BASE_URL}/portfolio/orders", json=payload, headers=headers())
    return r.json()

def scan_and_trade():
    """Core loop — runs every SCAN_INTERVAL seconds."""
    while True:
        try:
            logging.info("── Scanning markets ──")
            reset_daily_if_needed()

            r = requests.get(f"{BASE_URL}/markets?limit=200&status=open", headers=headers())
            markets = r.json().get("markets", [])

            for m in markets:
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

                # Kelly sizing
                k     = kelly(br["rate"], mid, edge["direction"])
                k_adj = k * KELLY_FRAC
                bet_$ = min(BANKROLL * k_adj, MAX_BET)
                bet_$ = round(bet_$, 2)

                if bet_$ < 1.0:
                    continue

                # Daily limit check
                if daily_spent["amount"] + bet_$ > DAILY_LIMIT:
                    logging.info(f"Daily limit reached (${daily_spent['amount']:.2f}), skipping {ticker}")
                    break

                # Don't double-bet same market today
                if any(t["ticker"] == ticker and t["date"] == str(date.today()) for t in trade_log):
                    continue

                log_entry = {
                    "date":      str(date.today()),
                    "time":      datetime.now().strftime("%H:%M:%S"),
                    "ticker":    ticker,
                    "title":     title[:60],
                    "signal":    edge["signal"],
                    "direction": edge["direction"],
                    "edge_pct":  edge["magnitude"],
                    "crowd":     round(mid * 100),
                    "base_rate": round(br["rate"] * 100),
                    "bet_$":     bet_$,
                    "status":    "PAPER" if not AUTO_ENABLED else "LIVE",
                    "result":    None
                }

                if AUTO_ENABLED:
                    price_cents = round(mid * 100) if edge["direction"] == "YES" else round((1 - mid) * 100)
                    quantity    = max(1, int(bet_$ / (price_cents / 100)))
                    order       = place_order(ticker, edge["direction"], price_cents, quantity)
                    log_entry["result"] = order
                    daily_spent["amount"] += bet_$
                    logging.info(f"PLACED {edge['direction']} {ticker} ${bet_$:.2f} — {order}")
                else:
                    logging.info(f"PAPER {edge['direction']} {ticker} ${bet_$:.2f} (auto disabled)")

                trade_log.append(log_entry)
                if len(trade_log) > 500:
                    trade_log.pop(0)

        except Exception as e:
            logging.error(f"Scan error: {e}")

        time.sleep(SCAN_INTERVAL)

# ── API routes ────────────────────────────────────────────────────

@app.route("/markets")
def markets():
    r = requests.get(f"{BASE_URL}/markets?limit=200&status=open", headers=headers())
    data = r.json()
    for m in data.get("markets", []):
        bid = m.get("yes_bid", 0) or 0
        ask = m.get("yes_ask", 0) or 0
        mid = ((bid + ask) / 2) / 100 if bid and ask else (bid or ask) / 100
        m["mid_price"] = round(mid, 4)
        br = get_base_rate(m.get("title", ""))
        if br:
            m["base_rate"] = br
            m["edge"]      = compute_edge(mid, br["rate"])
        else:
            m["base_rate"] = None
            m["edge"]      = None
    return jsonify(data)

@app.route("/trades")
def trades():
    return jsonify({
        "trades":       list(reversed(trade_log)),
        "daily_spent":  daily_spent,
        "auto_enabled": AUTO_ENABLED,
        "limits":       {"max_bet": MAX_BET, "daily_limit": DAILY_LIMIT, "min_edge": MIN_EDGE}
    })

@app.route("/status")
def status():
    return jsonify({
        "auto_enabled":   AUTO_ENABLED,
        "daily_spent":    daily_spent["amount"],
        "daily_limit":    DAILY_LIMIT,
        "daily_remaining": DAILY_LIMIT - daily_spent["amount"],
        "total_trades":   len(trade_log),
        "scan_interval":  SCAN_INTERVAL,
        "max_bet":        MAX_BET,
        "min_edge":       MIN_EDGE,
        "kelly_fraction": KELLY_FRAC,
        "bankroll":       BANKROLL
    })

if __name__ == "__main__":
    t = threading.Thread(target=scan_and_trade, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8080)
