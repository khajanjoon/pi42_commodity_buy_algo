import hashlib
import hmac
import json
import os
import time
import traceback
import random
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Tuple, Any
import requests
from dotenv import load_dotenv
import signal
import sys

# =========================
# CONFIGURATION
# =========================
# Symbol configuration
SYMBOL_CONFIG = {import socketio, requests, time, os, hashlib, hmac, threading, json, math
from dotenv import load_dotenv

# ========= CONFIG =========
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

BASE_URL = "https://fapi.pi42.com"
WS_URL = "https://fawss.pi42.com/"

SYMBOLS = ["XAGINR", "XAUINR", "XPTINR", "XPDINR"]

CAPITAL_PER_TRADE = 6000
DROP_PERCENT = 2
TP_PERCENT = 1
TRADE_COOLDOWN = 20

MIN_QTY = {
    "XAGINR": 0.067,
    "XAUINR": 0.002,
    "XPTINR": 0.003,
    "XPDINR": 0.004,
}

sio = socketio.Client(reconnection=True)

prices, positions, orders = {}, {}, {}
last_trade = {s: 0 for s in SYMBOLS}

# ========= SIGNATURE =========
def generate_signature(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def sign(query):
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

# ========= TARGET =========
def calculate_target(entry):
    return int(round(entry * (1 + TP_PERCENT / 100)))

# ========= QTY CALC =========
def calculate_order_qty(sym):
    price = prices.get(sym)
    if not price:
        return None
    step = MIN_QTY.get(sym, 0.001)
    raw = CAPITAL_PER_TRADE / price
    qty = math.floor(raw / step) * step
    return round(qty, 3) if qty >= step else None

# ========= ORDER DATA =========
def get_lowest_sell(sym):
    sells = [float(o["price"]) for o in orders.get(sym, [])
             if o["side"] == "SELL" and o.get("price")]
    return min(sells) if sells else None

def get_trigger_price(sym):
    lowest = get_lowest_sell(sym)
    if not lowest:
        return None
    return round(lowest * (1 - DROP_PERCENT / 100), 2)

# ========= PLACE ORDER =========
def place_market_buy(sym):
    qty = calculate_order_qty(sym)
    if not qty:
        return False

    timestamp = str(int(time.time() * 1000))
    tp = calculate_target(prices[sym])

    params = {
        "timestamp": timestamp,
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

    headers = {"api-key": API_KEY, "signature": signature, "Content-Type": "application/json"}

    r = requests.post(f"{BASE_URL}/v1/order/place-order", data=body, headers=headers, timeout=15)

    print(f"\n🚀 BUY {sym} | Qty:{qty} | TP:{tp}")
    print("Response:", r.text)
    return True

# ========= TRADE LOGIC =========
def trade_logic(sym):
    if sym not in prices:
        return
    if time.time() - last_trade[sym] < TRADE_COOLDOWN:
        return

    trigger = get_trigger_price(sym)
    if not trigger:
        return

    if prices[sym] <= trigger:
        print(f"📉 {sym} Trigger Hit → {prices[sym]} <= {trigger}")
        if place_market_buy(sym):
            last_trade[sym] = time.time()

# ========= FETCH POSITIONS =========
def fetch_positions_loop():
    while True:
        try:
            ts = str(int(time.time() * 1000))
            for sym in SYMBOLS:
                query = f"symbol={sym}&sortOrder=desc&pageSize=100&timestamp={ts}"
                headers = {"api-key": API_KEY, "signature": sign(query)}
                r = requests.get(f"{BASE_URL}/v1/positions/OPEN?{query}", headers=headers)
                data = r.json()
                positions[sym] = next((p for p in data if p["contractPair"] == sym), None)
        except Exception as e:
            print("Position error:", e)
        time.sleep(10)

# ========= FETCH ORDERS =========
def fetch_orders_loop():
    while True:
        try:
            ts = str(int(time.time() * 1000))
            query = f"timestamp={ts}"
            headers = {"api-key": API_KEY, "signature": sign(query)}
            r = requests.get(f"{BASE_URL}/v1/order/open-orders?{query}", headers=headers)
            data = r.json()
            for sym in SYMBOLS:
                orders[sym] = [o for o in data if o.get("symbol") == sym]
        except Exception as e:
            print("Order error:", e)
        time.sleep(12)

# ========= DASHBOARD =========
def display_loop():
    while True:
        print("\n========== DASHBOARD ==========")
        for sym in SYMBOLS:
            price = prices.get(sym)
            pos = positions.get(sym)
            lowest = get_lowest_sell(sym)
            trigger = get_trigger_price(sym)
            qty = calculate_order_qty(sym)

            print(f"\n🔹 {sym}")
            print(f"LTP: {price}")
            print(f"Lowest Open Sell: {lowest}")
            print(f"Trigger Price: {trigger}")
            print(f"Next Trade Qty: {qty}")

            if pos:
                entry = float(pos["entryPrice"])
                q = float(pos["quantity"])
                pnl = (price - entry) * q if price else 0
                print(f"Position → Qty:{q} Entry:{entry} PnL:{round(pnl,2)}")
            else:
                print("Position → None")

        time.sleep(4)

# ========= WEBSOCKET =========
@sio.event
def connect():
    print("WS Connected")
    sio.emit('subscribe', {'params': [f"{s.lower()}@markPrice" for s in SYMBOLS]})

@sio.on('markPriceUpdate')
def on_price(data):
    sym = data.get('s', '').upper()
    if sym in SYMBOLS:
        prices[sym] = float(data['p'])
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
        except Exception as e:
            print("WS reconnecting...", e)
            time.sleep(5)

    "XAGINR": {
        "qty": 1,
        "price_precision": 0,
        "qty_precision": 3,
        "avg_percent": 2,
        "target_percent": 1.0,
    },
    "XAUINR": {
        "qty": 0.01,
        "price_precision": 0,
        "qty_precision": 3,
        "avg_percent": 2,
        "target_percent": 1.0,
    },
    "XPDINR": {
        "qty": 0.033,
        "price_precision": 0,
        "qty_precision": 3,
        "avg_percent": 2,
        "target_percent": 1.0,
    },
    "XPTINR": {
        "qty": 0.027,
        "price_precision": 0,
        "qty_precision": 3,
        "avg_percent": 2,
        "target_percent": 1.0,
    },

}

# =========================
# SIGNATURE
# =========================
def generate_signature(secret: str, message: str) -> str:
    """Generate HMAC SHA256 signature."""
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

# =========================
# API ERROR HANDLER
# =========================
class APIErrorHandler:
    """Comprehensive API error handling with retry logic."""
    
    def __init__(self, max_retries: int = 3, backoff_factor: float = 1.5):
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.rate_limit_reset_times = {}
        
    def should_retry_error(self, error: Exception, http_status: int = None) -> bool:
        """Determine if an error should be retried."""
        # Network errors should be retried
        if isinstance(error, (requests.exceptions.ConnectionError, 
                             requests.exceptions.Timeout)):
            return True
            
        # Rate limiting should be retried
        if http_status == 429:
            return True
            
        # Server errors (5xx) should be retried
        if http_status and 500 <= http_status < 600:
            return True
            
        # Client errors (4xx) typically shouldn't be retried (except 429)
        if http_status and 400 <= http_status < 500:
            return False
            
        # Other errors
        if isinstance(error, requests.exceptions.RequestException):
            return True
            
        return False
    
    def get_retry_delay(self, attempt: int, http_status: int = None, retry_after: int = None) -> float:
        """Calculate retry delay with exponential backoff."""
        if retry_after:
            # Use server-specified retry time
            return float(retry_after)
        elif http_status == 429:
            # Rate limiting - use exponential backoff
            return min(30, self.backoff_factor ** attempt + random.uniform(0, 1))
        else:
            # Standard exponential backoff with jitter
            return min(10, self.backoff_factor ** attempt + random.uniform(0, 0.5))
    
    def handle_error(self, operation: str, error: Exception, attempt: int, 
                    http_status: int = None, retry_after: int = None) -> bool:
        """Handle API error with retry logic."""
        if attempt >= self.max_retries:
            print(f"❌ Max retries exceeded for {operation}")
            return False
        
        if not self.should_retry_error(error, http_status):
            print(f"❌ Non-retryable error for {operation}: {error}")
            return False
        
        delay = self.get_retry_delay(attempt, http_status, retry_after)
        
        error_type = "rate limit" if http_status == 429 else "server error" if http_status and http_status >= 500 else "error"
        print(f"⚠️ {operation} failed ({error_type}, attempt {attempt + 1}/{self.max_retries}): {error}")
        print(f"   Retrying in {delay:.1f} seconds...")
        
        time.sleep(delay)
        return True

# =========================
# API RATE LIMITER
# =========================
class RateLimiter:
    def __init__(self, calls_per_second: float = 2.0):
        self.calls_per_second = calls_per_second
        self.min_interval = 1.0 / calls_per_second
        self.last_call_time = 0
    
    def wait(self):
        """Wait if necessary to maintain rate limit."""
        current_time = time.time()
        elapsed = current_time - self.last_call_time
        
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed
            time.sleep(sleep_time)
        
        self.last_call_time = time.time()

# =========================
# AVERAGING BOT BASED ON OPEN TARGET/STOP ORDERS
# =========================
class AveragingBot:
    def __init__(self):
        load_dotenv()
        
        self.base_url = "https://fapi.pi42.com"
        self.market_url = "https://api.pi42.com"
        
        self.api_key = os.getenv("PI42_API_KEY")
        self.secret_key = os.getenv("PI42_API_SECRET")
        
        if not self.api_key or not self.secret_key:
            raise Exception("❌ API KEYS NOT FOUND - Please check your .env file")
        
        # ===== STRATEGY SETTINGS =====
        self.order_timeout = 300  # 5 minutes order timeout
        self.max_api_errors = 50  # Increased threshold
        
        # ===== STATE MANAGEMENT =====
        self.order_history = {}  # Track our order history
        self.last_update_time = datetime.now()
        self.is_running = True
        
        # Statistics
        self.total_orders_placed = 0
        self.total_orders_filled = 0
        self.total_orders_cancelled = 0
        self.total_pnl = 0
        
        # API error tracking
        self.api_error_count = 0
        self.consecutive_errors = 0
        
        # Initialize balance attributes
        self.balance = 0.0
        self.pnl_isolated = 0.0
        self.pnl_cross = 0.0
        self.available_balance = 0.0
        
        # ===== ERROR HANDLER =====
        self.error_handler = APIErrorHandler(max_retries=3, backoff_factor=1.5)
        
        # ===== RATE LIMITERS =====
        self.api_limiter = RateLimiter(calls_per_second=1.5)
        self.order_limiter = RateLimiter(calls_per_second=0.5)
        
        # ===== SIGNAL HANDLERS =====
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        # ===== INITIALIZE =====
        self._initialize_balance_and_positions()
        
        print("\n" + "="*60)
        print("🚀 PI42 AVERAGING BOT - TARGET ORDER BASED (NO STOP LOSS)")
        print(f"📅 Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"💰 Wallet Balance: {self.balance:.2f} INR")
        print(f"📈 Total PnL: {self.total_pnl:.2f} INR")
        print(f"📊 Trading {len(SYMBOL_CONFIG)} symbols")
        print("="*60 + "\n")
    
    def _handle_api_call(self, func, operation_name: str, *args, **kwargs):
        """Wrapper for API calls with error handling."""
        for attempt in range(self.error_handler.max_retries + 1):
            try:
                self.api_limiter.wait()
                result = func(*args, **kwargs)
                self.consecutive_errors = 0  # Reset on success
                return result
            except requests.exceptions.HTTPError as e:
                http_status = e.response.status_code if hasattr(e, 'response') else None
                retry_after = int(e.response.headers.get('Retry-After', 0)) if hasattr(e, 'response') else None
                
                if not self.error_handler.handle_error(operation_name, e, attempt, http_status, retry_after):
                    self.api_error_count += 1
                    self.consecutive_errors += 1
                    return None
            except requests.exceptions.RequestException as e:
                if not self.error_handler.handle_error(operation_name, e, attempt):
                    self.api_error_count += 1
                    self.consecutive_errors += 1
                    return None
            except Exception as e:
                print(f"❌ Unexpected error in {operation_name}: {e}")
                self.api_error_count += 1
                self.consecutive_errors += 1
                return None
        
        self.api_error_count += 1
        self.consecutive_errors += 1
        return None
    
    # =========================
    # INITIALIZATION
    # =========================
    def _initialize_balance_and_positions(self):
        """Initialize balance and fetch all positions with retry."""
        for attempt in range(5):  # More retries for initialization
            try:
                print(f"Initializing (attempt {attempt + 1})...")
                if not self._update_balance():
                    raise Exception("Failed to fetch balance")
                
                # Initialize order history
                for symbol in SYMBOL_CONFIG.keys():
                    self.order_history[symbol] = {
                        "open_orders": [],
                        "filled_orders": [],
                        "cancelled_orders": [],
                        "last_avg_price": None,
                        "total_quantity": 0,
                        "avg_entry_price": 0,
                    }
                
                # Fetch and analyze existing orders
                self.update_order_status()
                print("✅ Initialization complete")
                return
            except Exception as e:
                if attempt < 4:
                    print(f"⚠️ Initialization failed (attempt {attempt + 1}): {e}")
                    time.sleep(3)
                else:
                    print(f"❌ Failed to initialize after 5 attempts: {e}")
                    raise
    
    # =========================
    # OPEN ORDERS MANAGEMENT
    # =========================
    def fetch_open_orders(self, symbol: str = None) -> List[Dict]:
        """Fetch open orders from PI42 API with error handling."""
        
        def _fetch():
            timestamp = str(int(time.time() * 1000))
            params = {"timestamp": timestamp}
            signature = generate_signature(self.secret_key, f"timestamp={timestamp}")
            
            headers = {
                'api-key': self.api_key,
                'signature': signature,
            }
            
            open_orders_url = f"{self.base_url}/v1/order/open-orders"
            response = requests.get(open_orders_url, headers=headers, params=params, timeout=15)
            response.raise_for_status()
            
            if response.status_code == 200:
                response_data = response.json()
                if symbol:
                    filtered_orders = []
                    for order in response_data:
                        if order.get("symbol") == symbol:
                            filtered_orders.append(order)
                    return filtered_orders
                else:
                    return response_data
            elif response.status_code == 404:
                return []
            else:
                response.raise_for_status()
        
        result = self._handle_api_call(_fetch, "fetch_open_orders")
        if result is None:
            print(f"⚠️ Failed to fetch open orders after retries")
            return []  # Return empty list on complete failure
        return result
    
    def analyze_open_orders(self, symbol: str, open_orders: List[Dict]) -> Dict:
        """Analyze open orders for averaging strategy."""
        buy_limit_orders = []
        take_profit_orders = []
        
        print(f"\n🔍 Analyzing {len(open_orders)} open orders for {symbol}:")
        
        for order in open_orders:
            if order.get("symbol") != symbol:
                continue
            
            order_type = order.get("type", "").upper()
            side = order.get("side", "").upper()
            
            # Safely handle price (could be None for market orders)
            price_str = order.get("price")
            price = float(price_str) if price_str is not None else 0.0
            
            # Safely handle stop price (could be None for limit orders)
            stop_price_str = order.get("stopPrice")
            stop_price = float(stop_price_str) if stop_price_str is not None else 0.0
            
            if side == "BUY":
                if order_type == "LIMIT":
                    order_amount_str = order.get("orderAmount", "0")
                    filled_amount_str = order.get("filledAmount", "0")
                    
                    buy_limit_orders.append({
                        "order_id": order.get("clientOrderId") or order.get("linkId"),
                        "price": price,
                        "quantity": float(order_amount_str) if order_amount_str else 0.0,
                        "filled": float(filled_amount_str) if filled_amount_str else 0.0,
                        "time": order.get("time", ""),
                        "status": order.get("status", ""),
                    })
            elif side == "SELL":
                if stop_price > 0:
                    order_amount_str = order.get("orderAmount", "0")
                    
                    order_data = {
                        "order_id": order.get("clientOrderId") or order.get("linkId"),
                        "trigger_price": stop_price,
                        "price": price,
                        "quantity": float(order_amount_str) if order_amount_str else 0.0,
                        "order_type": order_type,
                    }
                    print(f"   Detected SELL order: {order_data}")
                    if "STOP_LIMIT" in order_type or "LIMIT" in order_type:
                        take_profit_orders.append(order_data)
        
        return {
            "buy_limits": buy_limit_orders,
            "take_profits": take_profit_orders,
            "has_position": len(take_profit_orders) > 0,
        }
    
    def get_lowest_take_profit_price(self, take_profit_orders: List[Dict]) -> Optional[float]:
        """Get the lowest take profit price from open orders."""
        if not take_profit_orders:
            return None
        
        try:
            valid_orders = [order for order in take_profit_orders 
                           if order.get("trigger_price") and order["trigger_price"] > 0]
            
            if not valid_orders:
                return None
            
            lowest_tp = min(valid_orders, key=lambda x: x["trigger_price"])
            return lowest_tp["trigger_price"]
        except Exception as e:
            print(f"⚠️ Error finding lowest TP price: {e}")
            return None
    
    def should_place_averaging_order(self, symbol: str, ltp: float, 
                                     lowest_tp_price: Optional[float],
                                     has_position: bool) -> Tuple[bool, float, bool]:
        """Determine if we should place an averaging order."""
        cfg = SYMBOL_CONFIG[symbol]
        avg_percent = cfg["avg_percent"]
        
        if has_position and lowest_tp_price is None:
            print(f"⚠️ {symbol}: Has position but no TP orders. Creating TP only...")
            return False, ltp, True
        
        if lowest_tp_price is None:
            print(f"💰 {symbol}: No position or TP orders, placing market order")
            return True, ltp, False
        
        target_price = lowest_tp_price * (1 - avg_percent / 100)
        current_drop = ((lowest_tp_price - ltp) / lowest_tp_price) * 100
        
        if current_drop > avg_percent:
            print(f"📉 {symbol}: LTP dropped {current_drop:.2f}% from TP {lowest_tp_price:.2f}")
            return True, target_price, False
        
        needed_drop = avg_percent - current_drop
        print(f"🎯 {symbol}: Close to target ({needed_drop:.2f}% more needed)")
        
        return False, target_price, False
    
    # =========================
    # ORDER MANAGEMENT
    # =========================
    def format_price(self, price: float, symbol: str) -> int:
        """Format price as integer."""
        cfg = SYMBOL_CONFIG.get(symbol, {})
        min_price = cfg.get("min_price", 1)
        
        if min_price > 1:
            rounded = round(price / min_price) * min_price
        else:
            rounded = round(price)
        
        return int(rounded)
    
    def calculate_take_profit_price(self, symbol: str, entry_price: float) -> int:
        """Calculate take profit price."""
        cfg = SYMBOL_CONFIG.get(symbol, {})
        target_percent = cfg.get("target_percent", 1.0) / 100
        
        tp_price = entry_price * (1 + target_percent)
        tp_price_int = self.format_price(tp_price, symbol)
        
        return tp_price_int
    
    def place_market_order_with_tp(self, symbol: str, qty: float, price: float) -> Optional[str]:
        """Place a MARKET order with take profit only (no stop loss)."""
        self.order_limiter.wait()
        
        def _place_order():
            cfg = SYMBOL_CONFIG.get(symbol, {})
            qty_precision = cfg.get("qty_precision", 3)
            formatted_qty = round(qty, qty_precision)
            timestamp = str(int(time.time() * 1000))
            
            payload = {
                "timestamp": timestamp,
                "placeType": "ORDER_FORM",
                "quantity": formatted_qty,
                "side": "BUY",
                "symbol": symbol,
                "type": "MARKET",
                "marginAsset": "INR",
                "deviceType": "WEB",
                "timeInForce": "GTC",
                "reduceOnly": False,
            }
            
            print(f"\n📤 Placing MARKET order for {symbol}:")
            print(f"   Type: MARKET BUY")
            print(f"   Qty: {formatted_qty}")
            
            signature = generate_signature(
                self.secret_key, json.dumps(payload, separators=(",", ":"))
            )
            
            headers = {
                "api-key": self.api_key,
                "signature": signature,
                "Content-Type": "application/json"
            }
            
            res = requests.post(
                f"{self.base_url}/v1/order/place-order",
                json=payload,
                headers=headers,
                timeout=15
            )
            res.raise_for_status()
            return res
        
        try:
            res = self._handle_api_call(_place_order, "place_market_order")
            if res is None:
                print(f"❌ Failed to place market order for {symbol} after retries")
                return None
            
            if res.status_code == 201:
                data = res.json()
                order_id = data.get("orderId") or data.get("clientOrderId")
                timestamp = str(int(time.time() * 1000))
                filled_price = float(data.get("price", 0)) if data.get("price") else 0
                cfg = SYMBOL_CONFIG.get(symbol, {})
                qty_precision = cfg.get("qty_precision", 3)
                formatted_qty = round(qty, qty_precision)
                
                print(f"✅ {symbol} MARKET ORDER PLACED")
                print(f"   Order ID: {order_id}")
                print(f"   Filled at: {filled_price:.2f}")
                
                if filled_price > 0:
                    self.place_take_profit_order_with_retry(symbol, formatted_qty, filled_price)
                
                order_info = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "type": "MARKET_BUY",
                    "quantity": formatted_qty,
                    "timestamp": timestamp,
                    "status": "FILLED" if filled_price > 0 else "OPEN",
                    "filled_price": filled_price,
                    "reduceOnly": True,
                }
                
                self.order_history[symbol]["filled_orders"].append(order_info)
                self.total_orders_placed += 1
                self.total_orders_filled += 1
                return order_id
            else:
                error_data = res.json() if res.content else {}
                print(f"❌ {symbol} Order failed: {res.status_code}")
                print(f"   Error: {error_data}")
                return None
                
        except Exception as e:
            print(f"❌ {symbol} Order error: {e}")
            return None
    
    def place_take_profit_order_with_retry(self, symbol: str, qty: float, entry_price: float) -> bool:
        """Place a take profit order with retry logic."""
        
        def _place_tp():
            cfg = SYMBOL_CONFIG.get(symbol, {})
            timestamp = str(int(time.time() * 1000))
            target_percent = cfg.get("target_percent", 1.0) / 100
            tp_price = entry_price * (1 + target_percent)
            tp_price_int = self.format_price(tp_price, symbol)
            
            if tp_price_int <= 0:
                tp_price_int = max(1, int(entry_price * 1.01))
            
            timestamp = str(int(time.time() * 1000))
            
            payload = {
                "timestamp": timestamp,
                "placeType": "ORDER_FORM",
                "quantity": qty,
                "side": "SELL",
                "symbol": symbol,
                "type": "STOP_LIMIT",
                "stopPrice": tp_price_int,
                "price": tp_price_int,
                "marginAsset": "INR",
                "deviceType": "WEB",
                "timeInForce": "GTC",
                "reduceOnly": True,
            }
            
            print(f"   Placing TP @ {tp_price_int}")
            
            signature = generate_signature(
                self.secret_key, json.dumps(payload, separators=(",", ":"))
            )
            
            headers = {
                "api-key": self.api_key,
                "signature": signature,
                "Content-Type": "application/json"
            }
            
            res = requests.post(
                f"{self.base_url}/v1/order/place-order",
                json=payload,
                headers=headers,
                timeout=15
            )
            res.raise_for_status()
            return res
        
        try:
            res = self._handle_api_call(_place_tp, "place_tp_order")
            if res is None:
                print(f"❌ Failed to place TP order for {symbol} after retries")
                return False
            
            if res.status_code == 201:
                print(f"   ✅ TP order placed")
                return True
            else:
                print(f"   ❌ TP order failed: {res.status_code}")
                if res.content:
                    try:
                        error_data = res.json()
                        print(f"   Error details: {error_data}")
                    except:
                        print(f"   Response: {res.text[:200]}")
                return False
                
        except Exception as e:
            print(f"   ❌ TP order error: {e}")
            return False
    
    def create_tp_for_existing_position(self, symbol: str, position_qty: float, entry_price: float) -> bool:
        """Create TP order only for an existing position."""
        print(f"\n🎯 Creating TP for existing {symbol} position:")
        print(f"   Position: {position_qty:.4f} @ {entry_price:.2f}")
        
        success = self.place_take_profit_order_with_retry(symbol, position_qty, entry_price)
        
        if success:
            print(f"✅ Successfully created TP order for {symbol}")
            return True
        else:
            print(f"❌ Failed to create TP order for {symbol}")
            return False
    
    # =========================
    # POSITION MANAGEMENT
    # =========================
    def get_position(self, symbol: str) -> Optional[Dict]:
        """Fetch position from API with error handling."""
        
        def _fetch_position():
            timestamp = str(int(time.time() * 1000))
            params = {"symbol": symbol, "timestamp": timestamp, "pageSize": 10}
            query = "&".join(f"{k}={v}" for k, v in params.items())
            signature = generate_signature(self.secret_key, query)
            
            headers = {
                "api-key": self.api_key,
                "signature": signature,
                "Content-Type": "application/json"
            }
            
            res = requests.get(
                f"{self.base_url}/v1/positions/OPEN",
                headers=headers,
                params=params,
                timeout=15
            )
            res.raise_for_status()
            return res
        
        try:
            res = self._handle_api_call(_fetch_position, "get_position")
            if res is None:
                print(f"⚠️ Failed to fetch position for {symbol} after retries")
                return None
            
            if res.status_code == 200:
                positions = res.json()
                for pos in positions:
                    if pos.get("contractPair") == symbol and float(pos.get("quantity", 0)) > 0:
                        return {
                            "quantity": float(pos.get("quantity", 0)),
                            "entry_price": float(pos.get("entryPrice", 0)),
                            "unrealized_pnl": float(pos.get("unrealisedPnl", 0)),
                        }
                return None
            else:
                print(f"⚠️ Failed to fetch position for {symbol}: {res.status_code}")
                return None
                
        except Exception as e:
            print(f"⚠️ Position API error for {symbol}: {e}")
            return None
    
    # =========================
    # MARKET DATA
    # =========================
    def fetch_ltp(self, symbol: str) -> float:
        """Fetch Last Traded Price with error handling."""
        
        def _fetch_ltp():
            res = requests.post(
                f"{self.market_url}/v1/market/klines",
                json={"pair": symbol, "interval": "1m", "limit": 1},
                timeout=10
            )
            res.raise_for_status()
            return res
        
        try:
            res = self._handle_api_call(_fetch_ltp, "fetch_ltp")
            if res is None:
                raise Exception(f"Failed to fetch LTP for {symbol} after retries")
            
            data = res.json()
            if not data:
                raise ValueError(f"No LTP data for {symbol}")
            
            ltp = float(data[-1]["close"])
            return ltp
            
        except Exception as e:
            print(f"⚠️ LTP fetch error for {symbol}: {e}")
            raise
    
    # =========================
    # BALANCE MANAGEMENT
    # =========================
    def _update_balance(self) -> bool:
        """Fetch and update balance information."""
        try:
            bal = self.get_user_balance_with_retry()
            if bal is None:
                print("⚠️ Failed to update balance, using cached values")
                if hasattr(self, 'balance'):
                    return False
                else:
                    raise Exception("No cached balance available")
            
            self.balance = bal["inr_balance"]
            self.pnl_isolated = bal["pnl_isolated"]
            self.pnl_cross = bal["pnl_cross"]
            self.total_pnl = self.pnl_isolated + self.pnl_cross
            self.available_balance = max(0, self.balance - abs(self.pnl_isolated))
            self.last_update_time = datetime.now()
            return True
            
        except Exception as e:
            print(f"⚠️ Failed to update balance: {e}")
            if hasattr(self, 'balance'):
                print(f"   Using cached balance: {self.balance:.2f} INR")
                return False
            else:
                raise
    
    def get_user_balance_with_retry(self) -> Optional[Dict]:
        """Fetch user balance with retry logic."""
        
        def _fetch_balance():
            timestamp = str(int(time.time() * 1000))
            query = f"marginAsset=INR&timestamp={timestamp}"
            signature = generate_signature(self.secret_key, query)
            
            headers = {
                "api-key": self.api_key,
                "signature": signature,
                "Content-Type": "application/json"
            }
            
            res = requests.get(
                f"{self.base_url}/v1/wallet/futures-wallet/details",
                headers=headers,
                params={"marginAsset": "INR", "timestamp": timestamp},
                timeout=15
            )
            res.raise_for_status()
            return res
        
        try:
            res = self._handle_api_call(_fetch_balance, "get_balance")
            if res is None:
                return None
            
            data = res.json()
            return {
                "inr_balance": float(data.get("inrBalance", 0)),
                "pnl_isolated": float(data.get("unrealisedPnlIsolated", 0)),
                "pnl_cross": float(data.get("unrealisedPnlCross", 0)),
            }
            
        except Exception as e:
            print(f"⚠️ Balance API error: {e}")
            return None
    
    # =========================
    # SIGNAL HANDLER
    # =========================
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        print(f"\n⚠️ Received signal {signum}, shutting down gracefully...")
        self.is_running = False
        time.sleep(1)
        sys.exit(0)
    
    # =========================
    # ORDER TRACKING
    # =========================
    def update_order_status(self):
        """Update order status from API."""
        for symbol in SYMBOL_CONFIG.keys():
            try:
                open_orders = self.fetch_open_orders(symbol)
                if open_orders is None:
                    print(f"⚠️ Skipping order status update for {symbol} due to API error")
                    continue
                    
                api_order_ids = set()
                for order in open_orders:
                    order_id = order.get("clientOrderId") or order.get("linkId")
                    if order_id:
                        api_order_ids.add(order_id)
                
                for order in list(self.order_history[symbol]["open_orders"]):
                    order_id = order["order_id"]
                    if order_id not in api_order_ids:
                        print(f"✅ {symbol}: Order {order_id} not found in API, marking as filled")
                        order["status"] = "FILLED"
                        self.order_history[symbol]["filled_orders"].append(order)
                        self.order_history[symbol]["open_orders"].remove(order)
                        self.total_orders_filled += 1
                
            except Exception as e:
                print(f"⚠️ Error updating order status for {symbol}: {e}")
    
    # =========================
    # MAIN LOOP
    # =========================
    def run(self):
        """Main trading loop."""
        iteration = 0
        
        print("="*60)
        print("Starting AVERAGING BOT - Market Orders Only")
        print("Strategy: Place market order when price drops 2% below lowest TP")
        print("Or create TP for existing positions without TP orders (NO STOP LOSS)")
        print("="*60 + "\n")
        
        while self.is_running:
            try:
                iteration += 1
                current_time = datetime.now()
                
                # Check for excessive errors
                if self.consecutive_errors > 20:
                    print(f"⚠️ Too many consecutive errors ({self.consecutive_errors}), waiting 30 seconds...")
                    time.sleep(30)
                    self.consecutive_errors = 0
                    continue
                
                print(f"🔄 Iteration {iteration} - {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
                
                # Update order status
                self.update_order_status()
                time.sleep(1)
                
                # Update balance
                if not self._update_balance():
                    print("⚠️ Using cached balance due to update failure")
                
                print(f"💰 Balance: {self.balance:.2f} INR | "
                      f"Available: {self.available_balance:.2f} INR | "
                      f"Total PnL: {self.total_pnl:+.2f} INR")
                print(f"📊 API Errors: {self.api_error_count} | "
                      f"Consecutive Errors: {self.consecutive_errors}")
                
                # Process each symbol
                for symbol, cfg in SYMBOL_CONFIG.items():
                    try:
                        qty = cfg["qty"]
                        
                        # Fetch market data
                        try:
                            ltp = self.fetch_ltp(symbol)
                            time.sleep(0.5)
                        except Exception as e:
                            print(f"⚠️ Failed to fetch LTP for {symbol}: {e}")
                            continue
                        
                        # Fetch position
                        position = self.get_position(symbol)
                        has_position = position is not None and position["quantity"] > 0
                        time.sleep(0.5)
                        
                        # Fetch and analyze open orders
                        open_orders = self.fetch_open_orders(symbol)
                        if open_orders is None:
                            print(f"⚠️ Skipping {symbol} due to API error fetching orders")
                            continue
                        
                        order_analysis = self.analyze_open_orders(symbol, open_orders)
                        lowest_tp_price = self.get_lowest_take_profit_price(
                            order_analysis["take_profits"]
                        )
                        
                        should_place, target_price, create_tp_only = self.should_place_averaging_order(
                            symbol, ltp, lowest_tp_price, has_position
                        )
                        
                        # Display status
                        position_qty = position["quantity"] if position else 0
                        position_avg = position["entry_price"] if position else 0
                        
                        status_line = (
                            f"\n📊 {symbol} Status:\n"
                            f"   LTP: {ltp:.2f}\n"
                            f"   Position: {position_qty:.3f} @ {position_avg:.2f}\n"
                        )
                        
                        if lowest_tp_price:
                            current_drop = ((lowest_tp_price - ltp) / lowest_tp_price) * 100
                            status_line += f"   Lowest TP: {lowest_tp_price:.2f}\n"
                            status_line += f"   Current drop from TP: {current_drop:.2f}%\n"
                            status_line += f"   Target drop needed: {cfg['avg_percent']}%\n"
                        else:
                            if has_position:
                                status_line += f"   Has position but no TP orders\n"
                            else:
                                status_line += f"   No position or TP orders found\n"
                        
                        status_line += f"   Should place market order: {should_place}"
                        if create_tp_only:
                            status_line += f"\n   Will create TP only (no stop loss)"
                        
                        print(status_line)
                        
                        # Handle different scenarios
                        if create_tp_only:
                            print(f"\n🎯 Creating TP for existing {symbol} position...")
                            success = self.create_tp_for_existing_position(
                                symbol, position_qty, position_avg
                            )
                            if success:
                                print(f"✅ TP created for {symbol}")
                            else:
                                print(f"❌ Failed to create TP for {symbol}")
                        
                        elif should_place:
                            print(f"\n🎯 {symbol}: Placing MARKET order with TP only")
                            order_id = self.place_market_order_with_tp(symbol, qty, ltp)
                            if order_id:
                                print(f"✅ Market order placed successfully!")
                                time.sleep(2)
                        
                        else:
                            if lowest_tp_price and has_position:
                                current_drop = ((lowest_tp_price - ltp) / lowest_tp_price) * 100
                                needed_drop = cfg["avg_percent"] - current_drop
                                if needed_drop > 0:
                                    print(f"⏳ {symbol}: Need {needed_drop:.2f}% more drop to place market order")
                            elif has_position:
                                print(f"✅ {symbol}: Position with TP active (no stop loss)")
                            else:
                                print(f"⏳ {symbol}: No position, waiting for conditions...")
                        
                        time.sleep(0.5)
                        
                    except Exception as e:
                        print(f"❌ Error processing {symbol}: {e}")
                        traceback.print_exc()
                        continue
                
                wait_time = 3
                print(f"\n⏳ Waiting {wait_time} seconds until next update...")
                for i in range(wait_time):
                    if not self.is_running:
                        break
                    time.sleep(1)
                    
            except KeyboardInterrupt:
                print("\n⚠️ Bot stopped by user")
                self.is_running = False
                break
                
            except Exception as e:
                print(f"\n❌ Critical error in iteration {iteration}: {e}")
                traceback.print_exc()
                time.sleep(30)

# =========================
# START
# =========================
if __name__ == "__main__":
    try:
        bot = AveragingBot()
        bot.run()
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
        traceback.print_exc()
        sys.exit(1)
