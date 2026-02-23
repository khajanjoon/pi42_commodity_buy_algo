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

# ========= LOAD ENV =========
load_dotenv(Path(__file__).parent / ".env")

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

if not API_KEY or not API_SECRET:
    print("❌ API keys not loaded")
    exit()

# ========= CONFIG =========
BASE_URL = "https://fapi.pi42.com"
WS_URL = "https://fawss.pi42.com/"

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

    return hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()


def sign(query):

    return hmac.new(
        API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

# ========= PRICE NORMALIZER =========
def normalize_price(sym, price):

    if sym.endswith("INR"):
        return int(round(price))

    return round(price, 2)

# ========= TARGET =========
def calculate_target(sym, entry):

    tp = entry * (1 + TP_PERCENT / 100)

    return normalize_price(sym, tp)

# ========= QTY =========
def calculate_order_qty(sym):

    price = prices.get(sym)

    if not price:
        return None

    step = MIN_QTY.get(sym, 0.001)

    raw = CAPITAL_PER_TRADE / price

    qty = math.floor(raw / step) * step

    return round(qty, 6)

# ========= TRIGGER BASED ON TP SELL =========
def get_lowest_tp_sell(sym):

    if sym not in orders:
        return None

    sells = [
        float(o["price"])
        for o in orders[sym]
        if o.get("side") == "SELL" and o.get("price")
    ]

    if sells:
        return min(sells)

    return None


def get_trigger_price(sym):

    tp_sell = get_lowest_tp_sell(sym)

    if tp_sell is None:
        return None

    trigger = tp_sell * (1 - RISE_PERCENT / 100)

    return normalize_price(sym, trigger)

# ========= PLACE BUY =========
def place_market_buy(sym):

    if sym not in prices:
        return False

    qty = calculate_order_qty(sym)

    if not qty:
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

    headers = {
        "api-key": API_KEY,
        "signature": signature,
        "Content-Type": "application/json"
    }

    try:

        r = requests.post(
            f"{BASE_URL}/v1/order/place-order",
            data=body,
            headers=headers
        )

        print(f"\n🟢 BUY {sym}")
        print(f"Qty: {qty}")
        print(f"Entry: {entry}")
        print(f"TP SELL: {tp}")
        print("Response:", r.text)

        return True

    except Exception as e:

        print("❌ Order error:", e)

        return False

# ========= TRADE LOGIC =========
def trade_logic(sym):

    if sym not in prices:
        return

    if time.time() - last_trade[sym] < TRADE_COOLDOWN:
        return

    pos = positions.get(sym)

    if not pos:

        print(f"⚡ FIRST BUY {sym}")

        if place_market_buy(sym):
            last_trade[sym] = time.time()

        return

    trigger = get_trigger_price(sym)

    if trigger is None:
        return

    if prices[sym] <= trigger:

        print(f"📉 Trigger BUY {sym} at {prices[sym]}")

        if place_market_buy(sym):
            last_trade[sym] = time.time()

# ========= FETCH POSITIONS =========
def fetch_positions_loop():

    while True:

        try:

            ts = str(int(time.time() * 1000))

            for sym in SYMBOLS:

                query = f"symbol={sym}&timestamp={ts}"

                headers = {
                    "api-key": API_KEY,
                    "signature": sign(query)
                }

                r = requests.get(
                    f"{BASE_URL}/v1/positions/OPEN?{query}",
                    headers=headers
                )

                if r.status_code == 200:

                    data = r.json()

                    positions[sym] = next(
                        (p for p in data if p["contractPair"] == sym),
                        None
                    )

        except Exception as e:

            print("Position error:", e)

        time.sleep(10)

# ========= FETCH OPEN ORDERS =========
def fetch_orders_loop():

    while True:

        try:

            ts = str(int(time.time() * 1000))

            query = f"timestamp={ts}"

            headers = {
                "api-key": API_KEY,
                "signature": sign(query)
            }

            r = requests.get(
                f"{BASE_URL}/v1/order/open-orders?{query}",
                headers=headers
            )

            if r.status_code == 200:

                data = r.json()

                for sym in SYMBOLS:

                    orders[sym] = [
                        o for o in data
                        if o["symbol"] == sym
                    ]

        except Exception as e:

            print("Orders error:", e)

        time.sleep(10)

# ========= DASHBOARD =========
def display_loop():

    while True:

        print("\n========== GRID DASHBOARD ==========")

        for sym in SYMBOLS:

            price = prices.get(sym)

            trigger = get_trigger_price(sym)

            qty = calculate_order_qty(sym)

            print(f"\n{sym}")
            print(f"LTP: {price}")
            print(f"Next BUY Trigger: {trigger}")
            print(f"Next Qty: {qty}")

            pos = positions.get(sym)

            if pos:

                entry = float(pos["entryPrice"])
                q = float(pos["quantity"])

                pnl = (price - entry) * q if price else 0

                print(f"Entry: {entry}")
                print(f"Qty: {q}")
                print(f"PnL: {round(pnl,2)}")

        time.sleep(5)

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

    if sym and price:

        prices[sym] = float(price)

        trade_logic(sym)

# ========= MAIN =========
if __name__ == "__main__":

    threading.Thread(target=fetch_positions_loop, daemon=True).start()
    threading.Thread(target=fetch_orders_loop, daemon=True).start()
    threading.Thread(target=display_loop, daemon=True).start()

    while True:

        try:
            sio.connect(WS_URL)
            sio.wait()
        except:
            time.sleep(5)
