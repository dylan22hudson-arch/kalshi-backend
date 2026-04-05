from flask import Flask, jsonify
from flask_cors import CORS
import requests, os, re, time, threading, logging, json
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
MIN_EDGE          = float(os.environ.get("MIN_EDGE", 0.08))
KELLY_FRAC        = float(os.environ.get("KELLY_FRAC", 0.25))
BANKROLL          = float(os.environ.get("BANKROLL", 500))
AUTO_ENABLED      = os.environ.get("AUTO_ENABLED", "false").lower() == "true"
AUTO_TRIGGER_EDGE = float(os.environ.get("AUTO_TRIGGER_EDGE", 0.20))
SCAN_INTERVAL     = int(os.environ.get("SCAN_INTERVAL", 180))
DAILY_TARGET      = float(os.environ.get("DAILY_TARGET", 25))
MAX_HOURS         = int(os.environ.get("MAX_HOURS", 72))
MIN_VOLUME        = float(os.environ.get("MIN_VOLUME", 100))

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

def save_trades():
    try:
        with open("/tmp/trade_log.json", "w") as f:
            json.dump(trade_log, f)
    except:
        pass

def load_trades():
    global trade_log
    try:
        with open("/tmp/trade_log.json", "r") as f:
            trade_log = json.load(f)
        logging.info(f"Loaded {len(trade_log)} trades from disk")
    except:
        trade_log = []

def refresh_sports_odds():
    global live_odds
    if not ODDS_API_KEY:
        logging.warning("No ODDS_API_KEY set")
        return
    new_odds = {}
    for sport in SPORTS:
        try:
            r = requests.get(
                f"{ODDS_URL}/sports/{sport}/odds",
                params={
                    "apiKey":     ODDS_API_KEY,
                    "regions":    "us",
                    "markets":    "h2h",
                    "oddsFormat": "decimal"
                },
                timeout=10
            )
            if r.status_code != 200:
                logging.error(f"Odds API {sport}: {r.status_code}")
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
            logging.error(f"Odds error {sport}: {e}")
    live_odds = new_odds
    logging.info(f"Live odds: {len(live_odds)} teams")

def refresh_weather():
    global live_weather
    new_weather = {}
    for city, (lat, lon) in CITY_COORDS.items():
        try:
            r = requests.get(
                WEATHER_URL,
                params={
                    "latitude":    lat,
                    "longitude":   lon,
                    "hourly":      "precipitation_probability",
                    "forecast_days": 1,
                    "timezone":    "auto"
                },
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
    logging.info(f"Weather: {len(live_weather)} cities")

def get_headers():
    return {"Authorization": f"Bearer {KALSHI_KEY}", "Content-Type": "application/json"}

def extract_team(title):
    t = title.lower()
    for pat in [
        r"will the (.+?) win",
        r"will (.+?) beat",
        r"will (.+?) defeat",
        r"^(.+?) to win",
    ]:
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
    """Get base rate from live APIs — sports odds and weather."""
    t = title.lower()

    # Sports — only match clear game winner markets
    if re.search(r"will the .+ win|will .+ beat |will .+ defeat ", t):
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

    # Weather — only match clear rain/snow markets
    if re.search(r"^will it rain|^will there be rain|^will .+ see rain|^will .+ get rain", t):
        city = extract_city(title)
        if city and city in live_weather:
            return live_weather[city], f"Live weather ({city})"

    return None, None

# Strict base rates — only match clearly financial/economic Kalshi markets
STATIC_BASE_RATES = {
    # S&P 500 daily close
    "spx_up":    {
        "pattern": r"^will the s.?p|^s.?p 500.*(close|end|finish).*(above|over|higher|up)",
        "rate":    0.52,
        "source":  "S&P daily 2000-2024"
    },
    "spx_down":  {
        "pattern": r"^will the s.?p|^s.?p 500.*(close|end|finish).*(below|under|lower|down)",
        "rate":    0.48,
        "source":  "S&P daily 2000-2024"
    },
    # Bitcoin daily
    "btc_up":    {
        "pattern": r"^will bitcoin|^will btc|^bitcoin price.*(above|over|higher)",
        "rate":    0.52,
        "source":  "BTC daily 2018-2024"
    },
    "btc_down":  {
        "pattern": r"^will bitcoin|^will btc|^bitcoin price.*(below|under|lower)",
        "rate":    0.48,
        "source":  "BTC daily 2018-2024"
    },
    # Nasdaq
    "nasdaq_up": {
        "pattern": r"^will the nasdaq|^nasdaq.*(close|end|finish).*(above|over|up)",
        "rate":    0.53,
        "source":  "Nasdaq daily 2000-2024"
    },
    "nasdaq_down":{
        "pattern": r"^will the nasdaq|^nasdaq.*(close|end|finish).*(below|under|down)",
        "rate":    0.47,
        "source":  "Nasdaq daily 2000-2024"
    },
    # Fed rate decisions
    "fed_hold":  {
        "pattern": r"^will the fed(eral reserve)?.*(hold|pause|keep|unchanged|no change)",
        "rate":    0.70,
        "source":  "FRED meetings 2000-2024"
    },
    "fed_cut":   {
        "pattern": r"^will the fed(eral reserve)?.*(cut|lower|reduce|decrease)",
        "rate":    0.30,
        "source":  "FRED meetings 2000-2024"
    },
    "fed_hike":  {
        "pattern": r"^will the fed(eral reserve)?.*(hike|raise|increase)",
        "rate":    0.05,
        "source":  "FRED meetings 2000-2024"
    },
    # Economic data
    "recession": {
        "pattern": r"^will (the us|the united states|there be a) recession",
        "rate":    0.15,
        "source":  "NBER 1945-2024"
    },
    "unemployment":{
        "pattern": r"^will (the )?(us )?unemployment.*(above|exceed|over|rise)",
        "rate":    0.35,
        "source":  "BLS 2000-2024"
    },
    "cpi_high":  {
        "pattern": r"^will (the )?(us )?cpi.*(above|exceed|over)",
        "rate":    0.40,
        "source":  "BLS CPI 2000-2024"
    },
    # Weather — only explicit rain/snow markets
    "rain_nyc":  {
        "pattern": r"^will it rain.*(new york|nyc)|^will (new york|nyc).*(see|get|have).*(rain|precipitation)",
        "rate":    0.28,
        "source":  "NOAA NYC 2000-2024"
    },
    "rain_la":   {
        "pattern": r"^will it rain.*(los angeles|la )|^will (los angeles|la ).*(rain|precipitation)",
        "rate":    0.12,
        "source":  "NOAA LA 2000-2024"
    },
    "rain_chicago":{
        "pattern": r"^will it rain.*(chicago)|^will chicago.*(rain|precipitation)",
        "rate":    0.30,
        "source":  "NOAA Chicago 2000-2024"
    },
    "rain_miami":{
        "pattern": r"^will it rain.*(miami)|^will miami.*(rain|precipitation)",
        "rate":    0.45,
        "source":  "NOAA Miami 2000-2024"
    },
    "snow":      {
        "pattern": r"^will (it snow|there be snow).*(inch|cm|accumul)",
        "rate":    0.18,
        "source":  "NOAA snowfall data"
    },
    # Hurricane
    "hurricane": {
        "pattern": r"^will.*(hurricane|tropical storm).*(hit|make landfall|strike|reach)",
        "rate":    0.35,
        "source":  "NOAA 1950-2024"
    },
    # Government shutdown
    "shutdown":  {
        "pattern": r"^will (the )?(us |federal )?government (shut down|shutdown|close)",
        "rate":    0.20,
        "source":  "CRS shutdown history"
    },
}

def get_base_rate(title):
    """Get best base rate — live data first, strict static fallback second."""
    # Try live data first
    live_rate, live_source = get_live_base_rate(title)
    if live_rate is not None:
        return {"rate": live_rate, "source": live_source, "live": True}

    # Try strict static patterns
    t = (title or "").lower()
    for key, data in STATIC_BASE_RATES.items():
        if re.search(data["pattern"], t):
            return {"rate": data["rate"], "source": data["source"], "live": False}

    return None

def get_bias_signal(mid, volume):
    """
    Favorite-longshot bias — only apply to markets with real volume.
    Requires MIN_VOLUME to filter out illiquid garbage markets.
    """
    if mid <= 0 or volume < MIN_VOLUME:
        return None
    if mid < 0.12:
        return {
            "rate":      mid * 0.55,
            "source":    "Longshot bias (research)",
            "live":      False,
            "bias_type": "longshot"
        }
    if mid > 0.88:
        return {
            "rate":      min(mid + (1 - mid) * 0.35, 0.97),
            "source":    "Favorite bias (research)",
            "live":      False,
            "bias_type": "favorite"
        }
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

def get_volume(market):
    return float(market.get("volume_24h") or market.get("volume") or 0)

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
    r = requests.post(
        f"{BASE_URL}/portfolio/orders",
        json=payload,
        headers=get_headers()
    )
    return r.json()

def scan_and_trade():
    last_odds_refresh    = 0
    last_weather_refresh = 0

    # Load immediately on startup
    logging.info("Loading live data on startup...")
    refresh_sports_odds()
    refresh_weather()
    last_odds_refresh    = time.time()
    last_weather_refresh = time.time()

    while True:
        try:
            now_ts = time.time()
            if now_ts - last_odds_refresh > 600:
                refresh_sports_odds()
                last_odds_refresh = now_ts
            if now_ts - last_weather_refresh > 1800:
                refresh_weather()
                last_weather_refresh = now_ts

            logging.info("Scanning Kalshi markets...")
            reset_daily_if_needed()

            r       = requests.get(
                f"{BASE_URL}/markets?limit=200&status=open",
                headers=get_headers()
            )
            markets = r.json().get("markets", [])
            logging.info(f"Total markets: {len(markets)}")

            for m in markets:
                ticker = m.get("ticker", "")
                title  = m.get("title", "")
                bid    = m.get("yes_bid", 0) or 0
                ask    = m.get("yes_ask", 0) or 0
                volume = get_volume(m)

                # Skip markets with no active price
                if bid <= 0 and ask <= 0:
                    continue

                mid = ((bid + ask) / 2) / 100 if bid and ask else (bid or ask) / 100
                if mid <= 0:
                    continue

                # Try base rate first
                br = get_base_rate(title)

                # Only use bias on liquid markets
                if not br:
                    br = get_bias_signal(mid, volume)

                if not br:
                    continue

                edge = compute_edge(mid, br["rate"])
                if edge["signal"] == "SKIP":
                    continue

                raw_edge = abs(mid - br["rate"])
                k        = kelly(br["rate"], mid, edge["direction"])
                bet_amt  = round(min(BANKROLL * k * KELLY_FRAC, MAX_BET), 2)

                if bet_amt < 1.0:
                    continue
                if daily_state["spent"] + bet_amt > DAILY_LIMIT:
                    logging.info("Daily limit hit")
                    break
                if any(
                    t["ticker"] == ticker and
                    t["date"] == str(date.today())
                    for t in trade_log
                ):
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
                    "volume":      volume,
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
                    logging.info(f"PAPER {edge['direction']} {ticker} ${bet_amt:.2f} edge={edge['magnitude']}%")
                    daily_state["trade_count"] += 1

                trade_log.append(log_entry)
                save_trades()
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
    r      = requests.get(
        f"{BASE_URL}/markets?limit=200&status=open",
        headers=get_headers()
    )
    result = []
    for m in r.json().get("markets", []):
        bid    = m.get("yes_bid", 0) or 0
        ask    = m.get("yes_ask", 0) or 0
        volume = get_volume(m)

        # Skip 0c markets entirely
        if bid <= 0 and ask <= 0:
            continue

        mid  = ((bid + ask) / 2) / 100 if bid and ask else (bid or ask) / 100
        m["mid_price"]   = round(mid, 4)
        m["short_dated"] = is_short_dated(m)
        m["hours_left"]  = get_hours_left(m)
        m["volume"]      = volume

        br = get_base_rate(m.get("title", ""))
        if not br:
            br = get_bias_signal(mid, volume)

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
        "min_volume":      MIN_VOLUME,
    })

if __name__ == "__main__":
    load_trades()
    threading.Thread(target=scan_and_trade, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
