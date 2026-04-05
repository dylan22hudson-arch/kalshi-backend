from flask import Flask, jsonify
from flask_cors import CORS
import requests, os, re, time, threading, logging
from datetime import datetime, date, timezone

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO)

KALSHI_KEY        = os.environ.get("KALSHI_API_KEY")
ODDS_API_KEY      = os.environ.get("ODDS_API_KEY")
BASE_URL          = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_URL          = "https://api.the-odds-api.com/v4"
WEATHER_URL       = "https://api.open-meteo.com/v1/forecast"

MAX_BET           = float(os.environ.get("MAX_BET", 25))
DAILY_LIMIT       = float(os.environ.get("DAILY_LIMIT", 100))
MIN_EDGE          = float(os.environ.get("MIN_EDGE", 0.10))
KELLY_FRAC        = float(os.environ.get("KELLY_FRAC", 0.25))
BANKROLL          = float(os.environ.get("BANKROLL", 500))
AUTO_ENABLED      = os.environ.get("AUTO_ENABLED", "false").lower() == "true"
AUTO_TRIGGER_EDGE = float(os.environ.get("AUTO_TRIGGER_EDGE", 0.20))
SCAN_INTERVAL     = int(os.environ.get("SCAN_INTERVAL", 180))
DAILY_TARGET      = float(os.environ.get("DAILY_TARGET", 25))
MAX_HOURS         = int(os.environ.get("MAX_HOURS", 72))
LONGSHOT_MAX      = float(os.environ.get("LONGSHOT_MAX", 0.15))
FAVORITE_MIN      = float(os.environ.get("FAVORITE_MIN", 0.85))

daily_state = {
    "date":        str(date.today()),
    "spent":       0.0,
    "profit":      0.0,
    "target_hit":  False,
    "trade_count": 0,
    "auto_count":  0
}
trade_log    = []
live_odds    = {}
live_weather = {}

SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "baseball_mlb",
    "icehockey_nhl",
]

CITY_COORDS = {
    "new york":    (40.71, -74.01),
    "nyc":         (40.71, -74.01),
    "los angeles": (34.05, -118.24),
    "chicago":     (41.88, -87.63),
    "houston":     (29.76, -95.37),
    "dallas":      (32.78, -96.80),
    "miami":       (25.77, -80.19),
    "seattle":     (47.61, -122.33),
    "boston":      (42.36, -71.06),
    "denver":      (39.74, -104.98),
}

def refresh_sports_odds():
    global live_odds
    if not ODDS_API_KEY:
        return
    new_odds = {}
    for sport in SPORTS:
        try:
            r = requests.get(
                f"{ODDS_URL}/sports/{sport}/odds",
                params={"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h", "oddsFormat": "decimal"},
                timeout=10
            )
            if r.status_code != 200:
                continue
            for game in r.json():
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                home_probs, away_probs = [], []
                for bk in game.get("bookmakers", []):
                    for market in bk.get("markets", []):
                        if market["key"] != "h2h":
                            continue
                        for outcome in market["outcomes"]:
                            dec  = outcome["price"]
                            prob = 1 / dec if dec > 0 else 0
                            if outcome["name"] == home:
                                home_probs.append(prob)
                            elif outcome["name"] == away:
                                away_probs.append(prob)
                if home_probs and away_probs:
                    avg_home = sum(home_probs) / len(home_probs)
                    avg_away = sum(away_probs) / len(away_probs)
                    total    = avg_home + avg_away
                    new_odds[home.lower()] = round(avg_home / total, 4)
                    new_odds[away.lower()] = round(avg_away / total, 4)
        except Exception as e:
            logging.error(f"Odds API error {sport}: {e}")
    live_odds = new_odds
    logging.info(f"Sports odds: {len(live_odds)} teams loaded")

def refresh_weather():
    global live_weather
    new_weather = {}
    for city, (lat, lon) in CITY_COORDS.items():
        try:
            r = requests.get(
                WEATHER_URL,
                params={"latitude": lat, "longitude": lon, "hourly": "precipitation_probability", "forecast_days": 1, "timezone": "auto"},
                timeout=10
            )
            if r.status_code != 200:
                continue
            probs = r.json().get("hourly", {}).get("precipitation_probability", [])
            if probs:
                new_weather[city] = round(sum(probs) / len(probs) / 100, 4)
        except Exception as e:
            logging.error(f"Weather error {city}: {e}")
    live_weather = new_weather
    logging.info(f"Weather: {len(live_weather)} cities loaded")

def get_headers():
    return {"Authorization": f"Bearer {KALSHI_KEY}", "Content-Type": "application/json"}

def extract_team(title):
    t = title.lower()
    for pat in [r"will the (.+?) win", r"will (.+?) beat", r"will (.+?) defeat"]:
        m = re.search(pat, t)
        if m:
            return m.group(1).strip()
    return None

def extract_city(title):
    t = title.lower()
    for city in CITY_COORDS:
        if city in t:
            return city
    return None

def get_live_base_rate(title):
    t = title.lower()
    if any(w in t for w in ["win", "beat", "defeat"]):
        team = extract_team(title)
        if team and live_odds:
            for known_team, prob in live_odds.items():
                if team in known_team or known_team in team:
                    return prob, f"Live odds ({known_team})"
            for word in team.split():
                if len(word) > 4:
                    for known_team, prob in live_odds.items():
                        if word in known_team:
                            return prob, f"Live odds (~{known_team})"
    if any(w in t for w in ["rain", "precipitation", "snow", "storm"]):
        city = extract_city(title)
        if city and city in live_weather:
            return live_weather[city], f"Live weather ({city})"
    return None, None

STATIC_BASE_RATES = {
    "spx_up":    {"pattern": r"s.?p.*(above|over|higher|up|close above|end above)",   "rate": 0.52, "source": "S&P daily 2000-2024"},
    "spx_down":  {"pattern": r"s.?p.*(below|under|lower|down|close below|end below)", "rate": 0.48, "source": "S&P daily 2000-2024"},
    "btc_up":    {"pattern": r"bitcoin|btc.*(above|over|higher|up)",                  "rate": 0.52, "source": "BTC daily 2018-2024"},
    "btc_down":  {"pattern": r"bitcoin|btc.*(below|under|lower|down)",                "rate": 0.48, "source": "BTC daily 2018-2024"},
    "nasdaq_up": {"pattern": r"nasdaq|qqq.*(above|over|higher|up)",                   "rate": 0.53, "source": "Nasdaq daily 2000-2024"},
    "oil_up":    {"pattern": r"(oil|crude|wti).*(above|over|up|higher)",              "rate": 0.51, "source": "WTI daily 2000-2024"},
    "rain_nyc":  {"pattern": r"rain.*(new york|nyc|ny)|new york.*rain",               "rate": 0.28, "source": "NOAA NYC 2000-2024"},
    "rain_la":   {"pattern": r"rain.*(los angeles|la )|los angeles.*rain",            "rate": 0.12, "source": "NOAA LA 2000-2024"},
    "snow":      {"pattern": r"snow.*(inch|cm|accumul)",                              "rate": 0.18, "source": "NOAA snowfall"},
    "nba_fav":   {"pattern": r"nba.*(win|beat|defeat)|will the .* win",              "rate": 0.62, "source": "NBA win rate 2000-2024"},
    "nfl_fav":   {"pattern": r"nfl.*(win|beat|defeat)",                              "rate": 0.58, "source": "NFL win rate 2000-2024"},
    "mlb_fav":   {"pattern": r"mlb.*(win|beat|defeat)",                              "rate": 0.55, "source": "MLB win rate 2000-2024"},
    "nhl_fav":   {"pattern": r"nhl.*(win|beat|defeat)",                              "rate": 0.57, "source": "NHL win rate 2000-2024"},
    "fed_hold":  {"pattern": r"fed.*(hold|pause|unchanged|no change)",               "rate": 0.70, "source": "FRED 2000-2024"},
    "fed_cut":   {"pattern": r"fed.*(cut|lower|reduce|pivot)",                       "rate": 0.30, "source": "FRED 2000-2024"},
    "recession": {"pattern": r"recession",                                            "rate": 0.15, "source": "NBER 1945-2024"},
    "shutdown":  {"pattern": r"government.*(shutdown|close)",                         "rate": 0.20, "source": "CRS history"},
    "nohitter":  {"pattern": r"no.hitter",                                            "rate": 0.25, "source": "Baseball Reference"},
    "hurricane": {"pattern": r"hurricane.*(gulf|florida|texas|landfall)",             "rate": 0.35, "source": "NOAA 1950-2024"},
}

def get_base_rate(title):
    live_rate, live_source = get_live_base_rate(title)
    if live_rate is not None:
        return {"rate": live_rate, "source": live_source, "live": True}
    t = (title or "").lower()
    for key, data in STATIC_BASE_RATES.items():
        if re.search(data["pattern"], t):
            return {"rate": data["rate"], "source": data["source"], "live": False}
    return None

def get_bias_signal(mid):
    if mid <= 0:
        return None
    if mid < LONGSHOT_MAX:
        return {"rate": mid * 0.6, "source": "Longshot bias", "live": False, "bias_type": "longshot"}
    if mid > FAVORITE_MIN:
        return {"rate": min(mid + (1 - mid) * 0.3, 0.97), "source": "Favorite bias", "live": False, "bias_type": "favorite"}
    return None

def is_short_dated(market):
    close_ts = market.get("close_time") or market.get("expiration_time")
    if not close_ts:
        return False
    try:
        if isinstance(close_ts, str):
            close_ts = close_ts.replace("Z", "+00:00")
            close_dt = datetime.fromisoformat(close_ts)
        else:
            close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
        hours_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        return 0 < hours_left <= MAX_HOURS
    except:
        return False

def get_hours_left(market):
    close_ts = market.get("close_time") or market.get("expiration_time")
    if not close_ts:
        return None
    try:
        if isinstance(close_ts, str):
            close_ts = close_ts.replace("Z", "+00:00")
            close_dt = datetime.fromisoformat(close_ts)
        else:
            close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)
        return round((close_dt - datetime.now(timezone.utc)).total_seconds() / 3600, 1)
    except:
        return None

def kelly(p_true, p_market, side):
    if side == "NO":
        p_true   = 1 - p_true
        p_market = 1 - p_market
    b = (1 - p_market) / p_market
    return max(0.0, (b * p_true - (1 - p_true)) / b)

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
    return round(bet_amt * market_price / (1 - market_price), 2)

def reset_daily_if_needed():
    today = str(date.today())
    if daily_state["date"] != today:
        daily_state.update({
            "date": today, "spent": 0.0, "profit": 0.0,
            "target_hit": False, "trade_count": 0, "auto_count": 0
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
    last_odds_refresh    = 0
    last_weather_refresh = 0
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_odds_refresh > 600:
                refresh_sports_odds()
                last_odds_refresh = now_ts
            if now_ts - last_weather_refresh > 1800:
                refresh_weather()
                last_weather_refresh = now_ts
            logging.info("scanning kalshi markets...")
            reset_daily_if_needed()
            r       = requests.get(f"{BASE_URL}/markets?limit=200&status=open", headers=get_headers())
            markets = r.json().get("markets", [])
            logging.info(f"total markets: {len(markets)}")
            for m in markets:
                ticker = m.get("ticker", "")
                title  = m.get("title", "")
                bid    = m.get("yes_bid", 0) or 0
                ask    = m.get("yes_ask", 0) or 0
                mid    = ((bid + ask) / 2) / 100 if bid and ask else (bid or ask) / 100
                if mid <= 0:
                    continue
                br = get_base_rate(title)
                if not br:
                    br = get_bias_signal(mid)
                if not br:
                    continue
                edge = compute_edge(mid, br["rate"])
                if edge["signal"] == "SKIP":
                    continue
                raw_edge = abs(mid - br["rate"])
                bet_amt  = round(min(BANKROLL * kelly(br["rate"], mid, edge["direction"]) * KELLY_FRAC, MAX_BET), 2)
                if bet_amt < 1.0:
                    continue
                if daily_state["spent"] + bet_amt > DAILY_LIMIT:
                    break
                if any(t["ticker"] == ticker and t["date"] == str(date.today()) for t in trade_log):
                    continue
                est_profit = estimate_profit(bet_amt, mid, edge["direction"])
                is_insane  = raw_edge >= AUTO_TRIGGER_EDGE
                should_bet = is_insane or AUTO_ENABLED
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
                    "source":      br["source"],
                    "live_data":   br.get("live", False),
                    "bet_amt":     bet_amt,
                    "est_profit":  est_profit,
                    "hours_left":  get_hours_left(m),
                    "insane_edge": is_insane,
                    "status":      "LIVE" if should_bet else "PAPER",
                    "result":      None
                }
                if should_bet:
                    price_cents = round(mid * 100) if edge["direction"] == "YES" else round((1 - mid) * 100)
                    quantity    = max(1, int(bet_amt / (price_cents / 100)))
                    order       = place_order(ticker, edge["direction"], price_cents, quantity)
                    log_entry["result"] = order
                    daily_state["spent"]       += bet_amt
                    daily_state["profit"]      += est_profit
                    daily_state["trade_count"] += 1
                    daily_state["target_hit"]   = daily_state["profit"] >= DAILY_TARGET
                    if is_insane:
                        daily_state["auto_count"] += 1
                    logging.info(f"PLACED {edge['direction']} {ticker} ${bet_amt:.2f}")
                else:
                    logging.info(f"PAPER {edge['direction']} {ticker} ${bet_amt:.2f}")
                    daily_state["trade_count"] += 1
                trade_log.append(log_entry)
                if len(trade_log) > 500:
                    trade_log.pop(0)
        except Exception as e:
            logging.error(f"Scan error: {e}")
        time.sleep(SCAN_INTERVAL)

@app.route("/")
def index():
    return app.send_static_file("dashboard.html")

@app.route("/markets")
def markets():
    r      = requests.get(f"{BASE_URL}/markets?limit=200&status=open", headers=get_headers())
    result = []
    for m in r.json().get("markets", []):
        bid  = m.get("yes_bid", 0) or 0
        ask  = m.get("yes_ask", 0) or 0
        mid  = ((bid + ask) / 2) / 100 if bid and ask else (bid or ask) / 100
        m["mid_price"]   = round(mid, 4)
        m["short_dated"] = is_short_dated(m)
        m["hours_left"]  = get_hours_left(m)
        br = get_base_rate(m.get("title", ""))
        if not br:
            br = get_bias_signal(mid)
        if br:
            m["base_rate"] = br
            m["edge"]      = compute_edge(mid, br["rate"])
            m["raw_edge"]  = round(abs(mid - br["rate"]) * 100)
            m["insane"]    = abs(mid - br["rate"]) >= AUTO_TRIGGER_EDGE
        else:
            m["base_rate"] = None
            m["edge"]      = None
            m["raw_edge"]  = 0
            m["insane"]    = False
        result.append(m)
    return jsonify({"markets": result})

@app.route("/trades")
def trades():
    return jsonify({
        "trades":       list(reversed(trade_log)),
        "daily_state":  daily_state,
        "daily_target": DAILY_TARGET,
        "auto_enabled": AUTO_ENABLED,
        "auto_trigger": AUTO_TRIGGER_EDGE,
        "live_teams":   len(live_odds),
        "live_cities":  len(live_weather),
    })

@app.route("/status")
def status():
    return jsonify({
        "auto_enabled":    AUTO_ENABLED,
        "auto_trigger":    AUTO_TRIGGER_EDGE,
        "daily_spent":     daily_state["spent"],
        "daily_profit":    daily_state["profit"],
        "daily_target":    DAILY_TARGET,
        "target_hit":      daily_state["target_hit"],
        "trade_count":     daily_state["trade_count"],
        "auto_count":      daily_state["auto_count"],
        "daily_limit":     DAILY_LIMIT,
        "daily_remaining": DAILY_LIMIT - daily_state["spent"],
        "max_bet":         MAX_BET,
        "min_edge":        MIN_EDGE,
        "kelly_fraction":  KELLY_FRAC,
        "bankroll":        BANKROLL,
        "scan_interval":   SCAN_INTERVAL,
        "max_hours":       MAX_HOURS,
        "live_teams":      len(live_odds),
        "live_cities":     len(live_weather),
        "longshot_max":    LONGSHOT_MAX,
        "favorite_min":    FAVORITE_MIN,
    })

if __name__ == "__main__":
    threading.Thread(target=scan_and_trade, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
