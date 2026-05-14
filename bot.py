import os
import time
import hmac
import hashlib
import requests
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BYBIT_API_KEY    = os.environ["BYBIT_API_KEY"]
BYBIT_API_SECRET = os.environ["BYBIT_API_SECRET"]
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL_MINUTES", "5")) * 60

TAKE_PROFIT_PCT = 1.5
STOP_LOSS_PCT   = 5.0
SYMBOL          = "SOLUSDT"
COIN            = "SOL"

# ── Bybit API ──────────────────────────────────────────────────────────────────
BASE_URL = "https://api.bybit.com"

def sign_request(params: dict) -> dict:
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    param_str = timestamp + BYBIT_API_KEY + recv_window + "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }

def get_price():
    url = f"{BASE_URL}/v5/market/tickers"
    params = {"category": "spot", "symbol": SYMBOL}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return float(r.json()["result"]["list"][0]["lastPrice"])

def get_klines():
    url = f"{BASE_URL}/v5/market/kline"
    params = {"category": "spot", "symbol": SYMBOL, "interval": "60", "limit": "50"}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()["result"]["list"]
    closes = [float(c[4]) for c in data]
    closes.reverse()
    return closes

def get_usdt_balance():
    url = f"{BASE_URL}/v5/account/wallet-balance"
    params = {"accountType": "UNIFIED"}
    headers = sign_request(params)
    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    coins = r.json()["result"]["list"][0]["coin"]
    for c in coins:
        if c["coin"] == "USDT":
            return float(c["availableToWithdraw"])
    return 0.0

def get_sol_balance():
    url = f"{BASE_URL}/v5/account/wallet-balance"
    params = {"accountType": "UNIFIED"}
    headers = sign_request(params)
    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    coins = r.json()["result"]["list"][0]["coin"]
    for c in coins:
        if c["coin"] == COIN:
            return float(c["availableToWithdraw"])
    return 0.0

def place_order(side, qty):
    url = f"{BASE_URL}/v5/order/create"
    body = {
        "category": "spot",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(round(qty, 2)),
    }
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    import json
    body_str = json.dumps(body)
    param_str = timestamp + BYBIT_API_KEY + recv_window + body_str
    signature = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }
    r = requests.post(url, headers=headers, data=body_str, timeout=10)
    r.raise_for_status()
    result = r.json()
    if result["retCode"] != 0:
        raise Exception(f"Bybit error: {result['retMsg']}")
    return result

# ── Indicadores ────────────────────────────────────────────────────────────────
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }, timeout=10)

# ── Estado ─────────────────────────────────────────────────────────────────────
in_trade  = False
buy_price = None
qty_bought = None

def main():
    global in_trade, buy_price, qty_bought

    print(f"[{datetime.now()}] Bybit SOL Bot iniciado.")
    send_telegram(
        "🤖 <b>Bybit SOL Bot activo</b>\n"
        f"🎯 Take profit: +{TAKE_PROFIT_PCT}% | 🛑 Stop loss: -{STOP_LOSS_PCT}%\n"
        "⚡ Ejecución automática en Bybit\n"
        "⏱ Monitoreando cada 5 minutos."
    )

    while True:
        try:
            closes = get_klines()
            price  = get_price()
            rsi    = calculate_rsi(closes)
            now    = datetime.now().strftime("%d/%m/%Y %H:%M")

            print(f"[{datetime.now()}] SOL ${price:.2f} | RSI: {rsi} | En trade: {in_trade}")

            if not in_trade:
                if rsi < 35:
                    # Comprar con el 90% del balance disponible
                    usdt_balance = get_usdt_balance()
                    usdt_to_use  = usdt_balance * 0.90
                    qty          = usdt_to_use / price

                    if usdt_to_use < 1:
                        send_telegram("⚠️ Balance insuficiente para operar.")
                    else:
                        place_order("Buy", qty)
                        buy_price  = price
                        qty_bought = qty
                        in_trade   = True
                        send_telegram(
                            f"🟢 <b>COMPRA EJECUTADA — SOL</b>\n\n"
                            f"💰 Precio: <b>${price:,.2f}</b>\n"
                            f"📦 Cantidad: <b>{qty:.4f} SOL</b>\n"
                            f"💵 Invertido: <b>${usdt_to_use:.2f} USDT</b>\n"
                            f"📊 RSI: <b>{rsi}</b>\n"
                            f"🎯 Take profit: <b>${price * (1 + TAKE_PROFIT_PCT/100):,.2f}</b>\n"
                            f"🛑 Stop loss:   <b>${price * (1 - STOP_LOSS_PCT/100):,.2f}</b>\n"
                            f"🕐 {now}"
                        )
            else:
                change_pct = ((price - buy_price) / buy_price) * 100

                if change_pct >= TAKE_PROFIT_PCT:
                    sol_balance = get_sol_balance()
                    place_order("Sell", sol_balance)
                    ganancia = sol_balance * price - sol_balance * buy_price
                    send_telegram(
                        f"💰 <b>VENTA EJECUTADA — GANANCIA</b>\n\n"
                        f"📈 Entrada: <b>${buy_price:,.2f}</b>\n"
                        f"📈 Salida:  <b>${price:,.2f}</b>\n"
                        f"✅ Ganancia: <b>+{change_pct:.2f}%</b>\n"
                        f"💵 Ganancia aprox: <b>+${ganancia:.2f} USDT</b>\n"
                        f"🕐 {now}"
                    )
                    in_trade   = False
                    buy_price  = None
                    qty_bought = None

                elif change_pct <= -STOP_LOSS_PCT:
                    sol_balance = get_sol_balance()
                    place_order("Sell", sol_balance)
                    perdida = sol_balance * buy_price - sol_balance * price
                    send_telegram(
                        f"🛑 <b>VENTA EJECUTADA — STOP LOSS</b>\n\n"
                        f"📉 Entrada: <b>${buy_price:,.2f}</b>\n"
                        f"📉 Salida:  <b>${price:,.2f}</b>\n"
                        f"❌ Pérdida: <b>{change_pct:.2f}%</b>\n"
                        f"💵 Pérdida aprox: <b>-${perdida:.2f} USDT</b>\n"
                        f"🕐 {now}"
                    )
                    in_trade   = False
                    buy_price  = None
                    qty_bought = None
                else:
                    print(f"  └─ En posición: {change_pct:+.2f}% desde ${buy_price:.2f}")

        except Exception as e:
            print(f"[ERROR] {e}")
            send_telegram(f"⚠️ Error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
