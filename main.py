from flask import Flask, jsonify
from flask_cors import CORS
import requests, os, re

app = Flask(__name__)
CORS(app)
KALSHI_KEY = os.environ.get("KALSHI_API_KEY")

BASE_RATES = {
    "fed_cut":      {"pattern": r"fed.*(cut|lower|reduce|pivot)", "rate": 0.30, "source": "FRED 2000-2024"},
    "fed_hike":     {"pattern": r"fed.*(hike|raise|increase)", "rate": 0.25, "source": "FRED 2000-2024"},
    "cpi_high":     {"pattern": r"cpi.*(above|exceed|over)\s*[45]", "rate": 0.18, "source": "BLS 2000-2024"},
    "recession":    {"pattern": r"recession", "rate": 0.15, "source": "NBER 1945-2024"},
    "shutdown":     {"pattern": r"government.*(shutdown|close)", "rate": 0.20, "source": "CRS history"},
    "nohitter":     {"pattern": r"no.hitter", "rate": 0.25, "source": "Baseball Reference"},
    "hurricane":    {"pattern": r"hurricane.*(gulf|florida|texas|landfall)", "rate": 0.35, "source": "NOAA 1950-2024"},
    "snowfall":     {"pattern": r"snow.*(inch|feet|cm)", "rate": 0.22, "source": "NOAA climate"},
    "billion_deal": {"pattern": r"acqui|merger|deal.*(billion)", "rate": 0.12, "source": "M&A base rates"},
}

def get_base_rate(title):
    t = (title or "").lower()
    for key, data in BASE_RATES.items():
        if re.search(data["pattern"], t):
            return {"rate": data["rate"], "source": data["source"], "matched": key}
    return None

def compute_edge(market_price, base_rate):
    diff = market_price - base_rate
    if diff > 0.08:
        return {"signal": "FADE", "direction": "NO", "magnitude": round(abs(diff) * 100)}
    elif diff < -0.08:
        return {"signal": "BET", "direction": "YES", "magnitude": round(abs(diff) * 100)}
    return {"signal": "SKIP", "direction": "NEUTRAL", "magnitude": round(abs(diff) * 100)}

@app.route("/markets")
def markets():
    headers = {"Authorization": f"Bearer {KALSHI_KEY}"}
    r = requests.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets?limit=200&status=open",
        headers=headers
    )
    data = r.json()
    markets = data.get("markets", [])

    for m in markets:
        bid = m.get("yes_bid", 0) or 0
        ask = m.get("yes_ask", 0) or 0
        mid = ((bid + ask) / 2) / 100 if bid and ask else (bid or ask) / 100
        m["mid_price"] = round(mid, 4)

        br = get_base_rate(m.get("title", ""))
        if br:
            m["base_rate"] = br
            m["edge"] = compute_edge(mid, br["rate"])
        else:
            m["base_rate"] = None
            m["edge"] = None

    return jsonify({"markets": markets})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
