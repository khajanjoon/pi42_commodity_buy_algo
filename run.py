import socketio
import requests
import time
import os
import hashlib
import hmac
import threading
import json
import math
from dotenv import load_dotenv
from pathlib import Path

# ========= LOAD ENV SAFELY =========
load_dotenv(Path(__file__).parent / ".env")

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

if not API_KEY or not API_SECRET:
    print("❌ API keys not loaded. Check .env file")
    exit()

# ========= CONFIG =========
BASE_URL = "https://fapi.pi42.com"
WS_URL = "https://fawss.pi42.com/"

# INR PAIRS
SYMBOLS = ["XPTINR", "XPDINR"]

CAPITAL_PER_TRADE = 10000
RISE_PERCENT = 3
TP_PERCENT = 1.5
TRADE_COOLDOWN = 20

MIN_QTY = {
    "XPTINR": 0.005,
    "XPDINR": 0.005,
}

# ========= GLOBAL STATE =========
sio = socketio.Client(reconnection=True)

prices = {}
positions = {}
orders = {}

last_trade = {s: 0 for s in SYMBOLS}

# ========= SIGNATURE =========
def generate_signature(secret, message):

    if not secret or not message:
        return None

    return hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()


def sign(query):

    if not API_SECRET or not query:
        return None

    return hmac.new(
        API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

# ========= PRICE NORMALIZER =========
def normalize_price(sym, price):

    # INR pairs require integer precision
    if sym.endswith("INR"):
        return int(round(price, 0))

    return round(price, 2)

# ========= TARGET =========
def calculate_target(sym, entry):

    tp = entry * (1 + TP_PERCENT / 100)

    return normalize_price(sym, tp)

# ========= QTY CALC =========
def calculate_order_qty(sym):

    price = prices.get(sym)

    if not price:
        return None

    step = MIN_QTY.get(sym, 0.001)

    raw = CAPITAL_PER_TRADE / price

    qty = math.floor(raw / step) * step

    return round(qty, 6) if qty >= step else None

# ========= ORDER DATA =========
def get_lowest_buy(sym):

    if sym not in orders:
        return None

    buys = [
        float(o["price"])
        for o in orders.get(sym, [])
        if o.get("side") == "BUY" and o.get("price")
    ]

    return min(buys) if buys else None


def get_trigger_price(sym):

    lowest = get_lowest_buy(sym)

    if not lowest:
        return None

    trigger = lowest * (1 - RISE_PERCENT / 100)

    return normalize_price(sym, trigger)

# ========= PLACE BUY ORDER =========
def place_market_buy(sym):

    if sym not in prices:

        print("❌ No price available")

        return False

    qty = calculate_order_qty(sym)

    if not qty:

        print("❌ Invalid qty")

        return False

    entry = normalize_price(sym, prices[sym])

    tp = calculate_target(sym, entry)

    params = {

        "timestamp": str(int(time.time() * 1000)),
        "placeType": "ORDER_FORM",
        "quantity": qty,
        "side": "BUY",
        "price": 0,
        "symbol": sym,
        "type": "MARKET",
        "reduceOnly": False,
        "marginAsset": "INR",
        "deviceType": "WEB",
        "userCategory": "EXTERNAL",
        "takeProfitPrice": tp
    }

    body = json.dumps(params, separators=(',', ':'))

    signature = generate_signature(API_SECRET, body)

    if not signature:

        print("❌ Signature failed")

        return False

    headers = {

        "api-key": API_KEY,
        "signature": signature,
        "Content-Type": "application/json"
    }

    try:

        r = requests.post(
            f"{BASE_URL}/v1/order/place-order",
            data=body,
            headers=headers,
            timeout=15
        )

        print(f"\n🟢 BUY {sym}")
        print(f"Qty: {qty}")
        print(f"Entry: {entry}")
        print(f"TP: {tp}")
        print("Response:", r.text)

        return True

    except Exception as e:

        print("❌ Order failed:", e)

        return False

# ========= TRADE LOGIC =========
def trade_logic(sym):

    if sym not in prices or prices[sym] is None:
        return

    if time.time() - last_trade[sym] < TRADE_COOLDOWN:
        return

    pos = positions.get(sym)

    # FIRST BUY
    if not pos:

        print(f"⚡ No position → Opening FIRST LONG {sym}")

        if place_market_buy(sym):

            last_trade[sym] = time.time()

        return

    # ADD BUY
    trigger = get_trigger_price(sym)

    if not trigger:
        return

    if prices[sym] <= trigger:

        print(f"📉 Drop trigger hit {sym} → {prices[sym]}")

        if place_market_buy(sym):

            last_trade[sym] = time.time()

# ========= FETCH POSITIONS =========
def fetch_positions_loop():

    while True:

        try:

            ts = str(int(time.time() * 1000))

            for sym in SYMBOLS:

                query = f"symbol={sym}&timestamp={ts}"

                signature = sign(query)

                if not signature:
                    continue

                headers = {
                    "api-key": API_KEY,
                    "signature": signature
                }

                r = requests.get(
                    f"{BASE_URL}/v1/positions/OPEN?{query}",
                    headers=headers,
                    timeout=15
                )

                if r.status_code != 200:

                    positions[sym] = None
                    continue

                data = r.json()

                if not data:

                    positions[sym] = None
                    continue

                positions[sym] = next(
                    (p for p in data if p.get("contractPair") == sym),
                    None
                )

        except Exception as e:

            print("❌ Position fetch error:", e)

        time.sleep(10)

# ========= DASHBOARD =========
def display_loop():

    while True:

        print("\n========== LONG DASHBOARD ==========")

        for sym in SYMBOLS:

            price = prices.get(sym)

            pos = positions.get(sym)

            trigger = get_trigger_price(sym)

            qty = calculate_order_qty(sym)

            print(f"\n🔹 {sym}")
            print(f"LTP: {price}")
            print(f"Trigger: {trigger}")
            print(f"Next Qty: {qty}")

            if pos:

                entry = float(pos.get("entryPrice", 0))
                q = float(pos.get("quantity", 0))

                pnl = (price - entry) * q if price else 0

                print(f"LONG → Qty:{q}")
                print(f"Entry:{entry}")
                print(f"PnL:{round(pnl,2)}")

            else:

                print("Position → None")

        time.sleep(4)

# ========= WEBSOCKET =========
@sio.event
def connect():

    print("✅ WS Connected")

    sio.emit(
        "subscribe",
        {"params": [f"{s.lower()}@markPrice" for s in SYMBOLS]}
    )


@sio.on("markPriceUpdate")
def on_price(data):

    sym = data.get("s", "").upper()

    price = data.get("p")

    if not sym or not price:
        return

    prices[sym] = float(price)

    trade_logic(sym)

# ========= MAIN =========
if __name__ == "__main__":

    threading.Thread(target=fetch_positions_loop, daemon=True).start()
    threading.Thread(target=display_loop, daemon=True).start()

    while True:

        try:

            sio.connect(WS_URL)

            sio.wait()

        except Exception as e:

            print("⚠ WS reconnecting:", e)

            time.sleep(5)
