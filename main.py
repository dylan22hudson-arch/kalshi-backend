from flask import Flask, jsonify
from flask_cors import CORS
import requests, os, time, threading, logging, asyncio
from datetime import datetime, date
from playwright.async_api import async_playwright

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO)

ODDS_API_KEY   = os.environ.get("ODDS_API_KEY")
BETSELITE_USER = os.environ.get("BETSELITE_USER", "DYLHUD")
BETSELITE_PASS = os.environ.get("BETSELITE_PASS")
ODDS_URL       = "https://api.the-odds-api.com/v4"
SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL", 180))
MIN_EDGE       = float(os.environ.get("MIN_EDGE", 0.001))
MAX_BET        = float(os.environ.get("MAX_BET", 5))
DAILY_LIMIT    = float(os.environ.get("DAILY_LIMIT", 25))
BANKROLL       = float(os.environ.get("BANKROLL", 200))
KELLY_FRAC     = float(os.environ.get("KELLY_FRAC", 0.25))
AUTO_ENABLED   = os.environ.get("AUTO_ENABLED", "false").lower() == "true"
DAILY_TARGET   = float(os.environ.get("DAILY_TARGET", 25))

daily_state = {
    "date":        str(date.today()),
    "spent":       0.0,
    "profit":      0.0,
    "target_hit":  False,
    "trade_count": 0,
    "win_count":   0,
    "loss_count":  0
}
trade_log   = []
sharp_lines = {}
edges_cache = []

SPORTS_MAP = {
    "basketball_nba":       "NBA",
    "baseball_mlb":         "MLB",
    "icehockey_nhl":        "NHL",
    "americanfootball_nfl": "NFL",
    "basketball_ncaab":     "NCAA Basketball",
}

SPORT_NAV = {
    "NBA":             ("BASKETBALL", "NBA"),
    "NCAA Basketball": ("BASKETBALL", "NCAA Basketball"),
    "MLB":             ("BASEBALL",   None),
    "NHL":             ("HOCKEY",     None),
    "NFL":             ("FOOTBALL",   None),
}

def reset_daily_if_needed():
    today = str(date.today())
    if daily_state["date"] != today:
        daily_state.update({
            "date": today, "spent": 0.0, "profit": 0.0,
            "target_hit": False, "trade_count": 0,
            "win_count": 0, "loss_count": 0
        })

def american_to_prob(american):
    try:
        a = float(american)
        return 100 / (a + 100) if a > 0 else abs(a) / (abs(a) + 100)
    except:
        return None

def kelly(p_true, p_market):
    if p_market >= 1 or p_market <= 0:
        return 0
    b = (1 - p_market) / p_market
    k = (b * p_true - (1 - p_true)) / b
    return max(0.0, k)

def refresh_sharp_lines():
    global sharp_lines
    logging.info(f"Refreshing sharp lines with key: {ODDS_API_KEY[:8] if ODDS_API_KEY else 'MISSING'}...")
    new_lines = {}
    for sport_key, sport_name in SPORTS_MAP.items():
        try:
            url = f"{ODDS_URL}/sports/{sport_key}/odds"
            params = {
                "apiKey":     ODDS_API_KEY,
                "regions":    "us",
                "markets":    "spreads,h2h",
                "oddsFormat": "american",
                "bookmakers": "pinnacle,draftkings,fanduel"
            }
            logging.info(f"Fetching {sport_name}...")
            r = requests.get(url, params=params, timeout=15)
            logging.info(f"{sport_name}: status={r.status_code} games={len(r.json()) if r.status_code==200 else 'error'}")
            if r.status_code != 200:
                logging.error(f"{sport_name} error: {r.text[:200]}")
                continue
            games = r.json()
            for game in games:
                home     = game.get("home_team", "")
                away     = game.get("away_team", "")
                game_key = f"{away}@{home}"
                commence = game.get("commence_time", "")
                if game_key not in new_lines:
                    new_lines[game_key] = {
                        "home":      home,
                        "away":      away,
                        "sport":     sport_name,
                        "commence":  commence,
                        "spread":    {},
                        "moneyline": {}
                    }
                for bk in game.get("bookmakers", []):
                    for market in bk.get("markets", []):
                        for outcome in market.get("outcomes", []):
                            key   = market["key"]
                            name  = outcome["name"]
                            price = outcome["price"]
                            point = outcome.get("point")
                            if key == "h2h":
                                if name not in new_lines[game_key]["moneyline"]:
                                    new_lines[game_key]["moneyline"][name] = []
                                new_lines[game_key]["moneyline"][name].append(price)
                            elif key == "spreads":
                                if name not in new_lines[game_key]["spread"]:
                                    new_lines[game_key]["spread"][name] = []
                                new_lines[game_key]["spread"][name].append({"price": price, "point": point})
        except Exception as e:
            logging.error(f"Sharp lines error {sport_key}: {e}")

    for gk, game in new_lines.items():
        for side, prices in game["moneyline"].items():
            game["moneyline"][side] = round(sum(prices) / len(prices))
        for side, lines in game["spread"].items():
            game["spread"][side] = {
                "price": round(sum(l["price"] for l in lines) / len(lines)),
                "point": round(sum(l["point"] for l in lines) / len(lines), 1)
            }

    sharp_lines = new_lines
    logging.info(f"Sharp lines loaded: {len(sharp_lines)} games total")

def compute_edges():
    global edges_cache
    edges = []
    for game_key, sharp in sharp_lines.items():
        home = sharp["home"]
        away = sharp["away"]

        for side, sharp_ml in sharp["moneyline"].items():
            sharp_p = american_to_prob(sharp_ml)
            if not sharp_p:
                continue
            book_p = sharp_p * 0.97
            edge   = sharp_p - book_p
            if edge >= MIN_EDGE:
                k       = kelly(sharp_p, book_p) * KELLY_FRAC
                bet_amt = round(min(BANKROLL * k, MAX_BET), 2)
                win_amt = round(
                    bet_amt * (100 / abs(sharp_ml)) if sharp_ml < 0
                    else bet_amt * sharp_ml / 100, 2
                )
                edges.append({
                    "game":      game_key,
                    "sport":     sharp["sport"],
                    "bet_type":  "moneyline",
                    "team":      side,
                    "home":      home,
                    "away":      away,
                    "book_odds": sharp_ml,
                    "sharp_odds":sharp_ml,
                    "edge_pct":  round(edge * 100, 2),
                    "bet_amt":   bet_amt,
                    "win_amt":   win_amt,
                    "commence":  sharp["commence"]
                })

        for side, sharp_spread in sharp["spread"].items():
            sharp_p = american_to_prob(sharp_spread["price"])
            if not sharp_p:
                continue
            book_p = sharp_p * 0.97
            edge   = sharp_p - book_p
            if edge >= MIN_EDGE:
                k       = kelly(sharp_p, book_p) * KELLY_FRAC
                bet_amt = round(min(BANKROLL * k, MAX_BET), 2)
                win_amt = round(
                    bet_amt * (100 / abs(sharp_spread["price"])) if sharp_spread["price"] < 0
                    else bet_amt * sharp_spread["price"] / 100, 2
                )
                edges.append({
                    "game":       game_key,
                    "sport":      sharp["sport"],
                    "bet_type":   "spread",
                    "team":       side,
                    "home":       home,
                    "away":       away,
                    "book_odds":  sharp_spread["price"],
                    "book_point": sharp_spread.get("point"),
                    "sharp_odds": sharp_spread["price"],
                    "edge_pct":   round(edge * 100, 2),
                    "bet_amt":    bet_amt,
                    "win_amt":    win_amt,
                    "commence":   sharp["commence"]
                })

    edges.sort(key=lambda x: x["edge_pct"], reverse=True)
    edges_cache = edges
    logging.info(f"Edges computed: {len(edges)}")
    return edges

async def place_bet_playwright(edge):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page    = await context.new_page()

            # Login
            logging.info("Logging into betselite...")
            await page.goto("https://betselite.com", timeout=30000)
            await page.wait_for_timeout(2000)

            await page.fill('input[placeholder="Login ID"]', BETSELITE_USER)
            await page.fill('input[placeholder="Password"]', BETSELITE_PASS)
            await page.click('button:has-text("LOGIN")')
            await page.wait_for_timeout(3000)
            logging.info("Logged in")

            # Navigate to sport
            sport    = edge["sport"]
            nav_info = SPORT_NAV.get(sport, ("BASKETBALL", None))
            await page.click(f'text={nav_info[0]}', timeout=5000)
            await page.wait_for_timeout(1000)
            if nav_info[1]:
                try:
                    await page.click(f'text={nav_info[1]}', timeout=3000)
                    await page.wait_for_timeout(1000)
                except:
                    pass

            # Find team and click odds
            team     = edge["team"]
            bet_type = edge["bet_type"]
            bet_amt  = edge["bet_amt"]
            logging.info(f"Looking for {team} {bet_type}...")
            await page.wait_for_timeout(2000)

            clicked = False
            rows = await page.query_selector_all("tr")
            for row in rows:
                text = await row.inner_text()
                if team.lower() in text.lower():
                    cells = await row.query_selector_all("td")
                    target_idx = 1 if bet_type == "spread" else 2
                    if len(cells) > target_idx:
                        await cells[target_idx].click()
                        clicked = True
                        break

            if not clicked:
                await browser.close()
                return {"status": "failed", "reason": f"{team} not found"}

            await page.wait_for_timeout(1500)

            # Enter amount
            amount_input = await page.query_selector('input[placeholder*="amount"], input[placeholder*="Enter another"]')
            if amount_input:
                await amount_input.click()
                await amount_input.fill(str(int(bet_amt)))
            else:
                # Use quick bet buttons
                if bet_amt <= 25:
                    btn = await page.query_selector('text=$25')
                elif bet_amt <= 50:
                    btn = await page.query_selector('text=$50')
                else:
                    btn = await page.query_selector('text=$100')
                if btn:
                    await btn.click()
                else:
                    await browser.close()
                    return {"status": "failed", "reason": "amount input not found"}

            await page.wait_for_timeout(500)

            # Confirm bet
            confirm = await page.query_selector('button:has-text("Confirm Bet")')
            if confirm:
                await confirm.click()
                await page.wait_for_timeout(2000)
                logging.info(f"BET PLACED: {team} {bet_type} ${bet_amt}")
                await browser.close()
                return {"status": "placed", "team": team, "bet_type": bet_type, "amount": bet_amt}
            else:
                await browser.close()
                return {"status": "failed", "reason": "confirm button not found"}

    except Exception as e:
        logging.error(f"Playwright error: {e}")
        return {"status": "error", "reason": str(e)}

def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(coro)
    loop.close()
    return result

def scan_and_bet():
    # Load sharp lines immediately on startup
    logging.info("Starting first scan immediately...")
    refresh_sharp_lines()
    compute_edges()

    last_refresh = time.time()

    while True:
        try:
            now_ts = time.time()
            if now_ts - last_refresh > 300:
                refresh_sharp_lines()
                compute_edges()
                last_refresh = now_ts

            reset_daily_if_needed()
            current_edges = edges_cache

            for edge in current_edges:
                if edge["bet_amt"] < 1.0:
                    continue
                if daily_state["spent"] + edge["bet_amt"] > DAILY_LIMIT:
                    logging.info("Daily limit hit")
                    break
                already_bet = any(
                    t["game"] == edge["game"] and
                    t["bet_type"] == edge["bet_type"] and
                    t["team"] == edge["team"] and
                    t["date"] == str(date.today())
                    for t in trade_log
                )
                if already_bet:
                    continue

                log_entry = {
                    "date":      str(date.today()),
                    "time":      datetime.now().strftime("%H:%M:%S"),
                    "game":      edge["game"],
                    "sport":     edge["sport"],
                    "bet_type":  edge["bet_type"],
                    "team":      edge["team"],
                    "book_odds": edge["book_odds"],
                    "sharp_odds":edge["sharp_odds"],
                    "edge_pct":  edge["edge_pct"],
                    "bet_amt":   edge["bet_amt"],
                    "win_amt":   edge["win_amt"],
                    "status":    "LIVE" if AUTO_ENABLED else "PAPER",
                    "result":    None
                }

                if AUTO_ENABLED:
                    result = run_async(place_bet_playwright(edge))
                    log_entry["result"] = result
                    if result.get("status") == "placed":
                        daily_state["spent"]       += edge["bet_amt"]
                        daily_state["trade_count"] += 1
                        daily_state["target_hit"]   = daily_state["profit"] >= DAILY_TARGET
                        logging.info(f"SUCCESS: {edge['team']} ${edge['bet_amt']:.2f}")
                    else:
                        logging.warning(f"FAILED: {result}")
                else:
                    logging.info(f"PAPER: {edge['team']} ${edge['bet_amt']:.2f} edge={edge['edge_pct']}%")
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

@app.route("/edges")
def edges():
    return jsonify({
        "edges":       edges_cache,
        "sharp_games": len(sharp_lines),
        "timestamp":   datetime.now().strftime("%H:%M:%S")
    })

@app.route("/trades")
def trades():
    return jsonify({
        "trades":      list(reversed(trade_log)),
        "daily_state": daily_state,
        "daily_target":DAILY_TARGET,
        "auto_enabled":AUTO_ENABLED,
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
        "sharp_games":     len(sharp_lines),
    })

if __name__ == "__main__":
    threading.Thread(target=scan_and_bet, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
