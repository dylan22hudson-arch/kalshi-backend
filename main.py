from flask import Flask, jsonify
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)

KALSHI_KEY = os.environ.get("KALSHI_API_KEY")

@app.route("/markets")
def markets():
    headers = {"Authorization": f"Bearer {KALSHI_KEY}"}
    r = requests.get(
        "https://api.elections.kalshi.com/trade-api/v2/markets?limit=200&status=open",
        headers=headers
    )
    return jsonify(r.json())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
