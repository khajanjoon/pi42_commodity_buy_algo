import socketio
import requests
import time
import os
import hashlib
import hmac
import threading
import json
import math
import logging
import random
from dotenv import load_dotenv
from pathlib import Path

# ================= ENV =================
load_dotenv(Path(__file__).parent / ".env")

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

if not API_KEY or not API_SECRET:
    raise Exception("❌ API keys missing. Check your .env file.")

BASE_URL = "https://fapi.pi42.com"
WS_URL   = "https://fawss.pi42.com/"

# ================= CONFIG =================
SYMBOLS = ["XPTINR" , "XPDINR"]

CAPITAL_PER_TRADE = 15000

DROP_PERCENT           = 3       # price must DROP this % below lowest TP sell to trigger averaging
TP_PERCENT             = 1.5
TRADE_COOLDOWN         = 20      # seconds between trades per symbol
POSITION_SYNC_INTERVAL = 5       # seconds
ORDER_SYNC_INTERVAL    = 8       # seconds
MAX_RETRIES            = 3       # API call retries
REQUEST_TIMEOUT        = 10      # seconds

MIN_QTY = {
    "XPTINR": 0.005,
    "XPDINR": 0.005,
}

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ================= STATE =================
sio = socketio.Client(reconnection=True)

prices             = {s: None  for s in SYMBOLS}
positions          = {s: None  for s in SYMBOLS}
open_orders_cache  = {s: []    for s in SYMBOLS}
last_trade         = {s: 0     for s in SYMBOLS}
last_trigger_price = {s: None  for s in SYMBOLS}
active_order_flag  = {s: False for s in SYMBOLS}
positions_ready    = False

# Track placed order IDs to prevent duplicates (Pi42 idempotency key)
placed_order_ids   = {s: set() for s in SYMBOLS}

lock = threading.Lock()

# ================= HELPERS =================
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


def safe_request(method, url, **kwargs):
    """Retries failed HTTP requests up to MAX_RETRIES times."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method, url, timeout=REQUEST_TIMEOUT, **kwargs
            )
            return resp
        except requests.RequestException as e:
            log.warning(f"Request failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)   # exponential back-off
    return None


def normalize_price(sym, price):
    if sym.endswith("INR"):
        return int(round(price))
    return round(price, 2)


def calculate_order_qty(sym):
    price = prices.get(sym)
    if not price:
        return None
    step = MIN_QTY.get(sym, 0.001)
    raw  = CAPITAL_PER_TRADE / price
    qty  = math.floor(raw / step) * step
    return round(qty, 6)


def generate_client_order_id(sym):
    """Generate a unique client order ID using timestamp + random string for idempotency."""
    # Format: SYM_UnixTimestamp_Random4Digit
    unique_id = f"{sym}_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    return unique_id


def verify_order_placed(sym, client_order_id):
    """Verify if an order with given client order ID was already placed."""
    with lock:
        return client_order_id in placed_order_ids[sym]


def mark_order_placed(sym, client_order_id):
    """Mark an order as placed to prevent duplicates."""
    with lock:
        placed_order_ids[sym].add(client_order_id)
        # Keep only last 100 order IDs to prevent memory leak
        if len(placed_order_ids[sym]) > 100:
            # Remove oldest entries
            old_ids = list(placed_order_ids[sym])[:50]
            for old_id in old_ids:
                placed_order_ids[sym].discard(old_id)

# ================= SYNC POSITIONS =================
def sync_positions():
    global positions_ready
    try:
        ts     = str(int(time.time() * 1000))
        loaded = 0

        for sym in SYMBOLS:
            query   = f"symbol={sym}&timestamp={ts}"
            headers = {"api-key": API_KEY, "signature": sign(query)}
            resp    = safe_request("GET", f"{BASE_URL}/v1/positions/OPEN?{query}", headers=headers)

            if resp and resp.status_code == 200:
                data = resp.json()
                with lock:
                    positions[sym] = next(
                        (p for p in data if p["contractPair"] == sym), None
                    )
                loaded += 1
            else:
                log.error(f"Position sync failed for {sym}: {getattr(resp, 'text', 'no response')}")

        if loaded == len(SYMBOLS):
            positions_ready = True

    except Exception as e:
        log.error(f"Position sync error: {e}")


def position_sync_loop():
    while True:
        sync_positions()
        time.sleep(POSITION_SYNC_INTERVAL)

# ================= ORDER CACHE =================
def fetch_open_orders_loop():
    while True:
        try:
            ts      = str(int(time.time() * 1000))
            query   = f"timestamp={ts}"
            headers = {"api-key": API_KEY, "signature": sign(query)}
            resp    = safe_request("GET", f"{BASE_URL}/v1/order/open-orders?{query}", headers=headers)

            if resp and resp.status_code == 200:
                data = resp.json()
                with lock:
                    for sym in SYMBOLS:
                        open_orders_cache[sym] = [
                            o for o in data
                            if  o["symbol"] == sym
                            and o.get("side") == "SELL"
                        ]
            else:
                log.warning("Open orders fetch returned unexpected response.")

        except Exception as e:
            log.error(f"Order fetch error: {e}")

        time.sleep(ORDER_SYNC_INTERVAL)


def get_lowest_open_sell(sym):
    sell_prices = [
        float(o["price"])
        for o in open_orders_cache.get(sym, [])
        if o.get("price")
    ]
    return min(sell_prices) if sell_prices else None

# ================= PLACE LONG =================
def place_long(sym, client_order_id=None):
    """Places a market buy with an inline TP sell. Returns True on success."""
    entry = prices[sym]
    if entry is None:
        return False

    tp    = normalize_price(sym, entry * (1 + TP_PERCENT / 100))
    entry = normalize_price(sym, entry)
    qty   = calculate_order_qty(sym)

    if not qty:
        log.error(f"❌ Could not calculate qty for {sym}")
        return False

    # Generate client order ID if not provided (for idempotency)
    if not client_order_id:
        client_order_id = generate_client_order_id(sym)

    params = {
        "timestamp":       str(int(time.time() * 1000)),
        "placeType":       "ORDER_FORM",
        "quantity":        qty,
        "side":            "BUY",
        "price":           0,
        "symbol":          sym,
        "type":            "MARKET",
        "reduceOnly":      False,
        "marginAsset":     "INR",
        "deviceType":      "WEB",
        "userCategory":    "EXTERNAL",
        "takeProfitPrice": tp,
        "clientOrderId":   client_order_id  # Idempotency key to prevent duplicates
    }

    body      = json.dumps(params, separators=(',', ':'))
    signature = generate_signature(API_SECRET, body)
    headers   = {
        "api-key":      API_KEY,
        "signature":    signature,
        "Content-Type": "application/json"
    }

    resp = safe_request("POST", f"{BASE_URL}/v1/order/place-order", headers=headers, data=body)

    if not resp:
        log.error(f"❌ Long order failed {sym}: no response")
        return False, client_order_id

    if resp.status_code == 200:
        # Mark order as placed to prevent duplicates
        mark_order_placed(sym, client_order_id)
        log.info(f"🟢 LONG  {sym} | Entry: {entry} | TP: {tp} | Qty: {qty} | ClientID: {client_order_id}")
        return True, client_order_id

    log.error(f"❌ Long order rejected {sym}: {resp.text}")
    return False, client_order_id

# ================= TRADE LOGIC =================
# Track pending orders to prevent duplicates
pending_orders = {s: False for s in SYMBOLS}

def trade_logic(sym):
    global pending_orders
    
    # Read shared state under lock, then release before any HTTP call
    with lock:
        if not positions_ready:
            return
        if prices[sym] is None:
            return
        if active_order_flag[sym]:
            return
        if pending_orders[sym]:  # Don't place if order is pending confirmation
            return
        if time.time() - last_trade[sym] < TRADE_COOLDOWN:
            return

        pos = positions.get(sym)
        if pos and float(pos.get("quantity", 0)) == 0:
            positions[sym] = None
            pos = None

        current_price = prices[sym]
        orders        = list(open_orders_cache[sym])   # snapshot
        active_order_flag[sym] = True                  # reserve slot

    # ── All HTTP work happens OUTSIDE the lock ──────────────────────────
    try:
        # FIRST ENTRY
        if not pos:
            if orders:
                return
            log.info(f"📈 Opening FIRST LONG {sym}")
            
            # Generate client order ID BEFORE placing to prevent duplicates
            client_order_id = generate_client_order_id(sym)
            
            # Check if this exact order was already placed
            if verify_order_placed(sym, client_order_id):
                log.warning(f"⚠️ Order already placed for {sym}, skipping")
                return
            
            # Mark as pending BEFORE placing order
            with lock:
                pending_orders[sym] = True
            
            success, _ = place_long(sym, client_order_id)
            with lock:
                if success:
                    last_trade[sym] = time.time()
                pending_orders[sym] = False  # Clear pending after response
            return

        # LADDER ENTRY
        lowest_sell = get_lowest_open_sell(sym)
        if not lowest_sell:
            return

        trigger = normalize_price(sym, lowest_sell * (1 - DROP_PERCENT / 100))

        with lock:
            if last_trigger_price[sym] == trigger:
                return

        if current_price <= trigger:
            log.info(f"📉 {sym} Drop trigger hit → Averaging LONG")
            
            # Generate client order ID BEFORE placing to prevent duplicates
            client_order_id = generate_client_order_id(sym)
            
            # Check if this exact order was already placed
            if verify_order_placed(sym, client_order_id):
                log.warning(f"⚠️ Order already placed for {sym}, skipping")
                return
            
            # Mark as pending BEFORE placing order
            with lock:
                pending_orders[sym] = True
            
            success, _ = place_long(sym, client_order_id)
            with lock:
                if success:
                    last_trade[sym]         = time.time()
                    last_trigger_price[sym] = trigger
                pending_orders[sym] = False  # Clear pending after response

    finally:
        with lock:
            active_order_flag[sym] = False

# ================= DASHBOARD =================
def dashboard_loop():
    while True:
        time.sleep(5)

        print("\n" * 2)
        print("=" * 80)
        print("🔥 PI42 LONG ENGINE - LIVE DASHBOARD 🔥")
        print("=" * 80)

        total_unrealized = 0
        total_exposure   = 0

        with lock:
            for sym in SYMBOLS:
                price = prices.get(sym)
                pos   = positions.get(sym)

                print("\n------------------------------------------------------------")
                print(f"SYMBOL: {sym}")
                print("------------------------------------------------------------")
                print(f"Mark Price        : {price}")

                if not pos or not price:
                    print("Position          : None")
                    continue

                size  = float(pos["quantity"])
                entry = float(pos["entryPrice"])

                unrealized = (price - entry) * abs(size)
                exposure   = abs(size) * price

                lowest_sell = get_lowest_open_sell(sym)
                trigger     = None
                if lowest_sell:
                    trigger = normalize_price(sym, lowest_sell * (1 - DROP_PERCENT / 100))

                tp_price = normalize_price(sym, entry * (1 + TP_PERCENT / 100))

                print(f"Direction         : LONG")
                print(f"Position Size     : {size}")
                print(f"Entry Price       : {entry}")
                print(f"Take Profit       : {tp_price}")
                print(f"Unrealized PnL    : {round(unrealized, 4)}")
                print(f"Current Exposure  : {round(exposure, 4)}")
                print(f"Lowest SELL TP    : {lowest_sell}")
                print(f"Next Trigger      : {trigger}")

                total_unrealized += unrealized
                total_exposure   += exposure

        print("\n============================================================")
        print("PORTFOLIO SUMMARY")
        print("============================================================")
        print(f"Total Unrealized PnL : {round(total_unrealized, 4)}")
        print(f"Total Exposure       : {round(total_exposure, 4)}")
        print("=" * 80)

# ================= WEBSOCKET =================
@sio.event
def connect():
    log.info("✅ WebSocket connected & subscribed")
    sio.emit(
        "subscribe",
        {"params": [f"{s.lower()}@markPrice" for s in SYMBOLS]}
    )


@sio.on("markPriceUpdate")
def on_price(data):
    try:
        sym   = data.get("s", "").upper()
        price = data.get("p")
        if sym in SYMBOLS and price:
            prices[sym] = float(price)
            threading.Thread(target=trade_logic, args=(sym,), daemon=True).start()
    except Exception as e:
        log.error(f"on_price error: {e}")


@sio.event
def connect_error(data):
    log.error(f"WebSocket connection error: {data}")


@sio.event
def disconnect():
    log.warning("WebSocket disconnected")


def start_ws():
    while True:
        try:
            log.info("Connecting WebSocket...")
            sio.connect(WS_URL, transports=["websocket"])
            sio.wait()
        except Exception as e:
            log.error(f"WebSocket crashed: {e}")
        log.info("Reconnecting in 5s...")
        time.sleep(5)

# ================= MAIN =================
if __name__ == "__main__":
    sync_positions()   # initial sync before threads start

    threading.Thread(target=position_sync_loop,    daemon=True).start()
    threading.Thread(target=fetch_open_orders_loop, daemon=True).start()
    threading.Thread(target=dashboard_loop,         daemon=True).start()

    start_ws()
