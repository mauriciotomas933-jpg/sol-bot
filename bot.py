import os
import time
import hmac
import hashlib
import json
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
COINGECKO_ID    = "solana"

BASE_URL = "https://api.bybit.com"

# ── CoinGecko: precio + RSI ────────────────────────────────────────────────────
def get_coingecko_data():
    url = f"https://api.coingecko.com/api/v3/coins/{COINGECKO_ID}/market_chart"
    params = {"vs_currency": "usd", "days": "2", "interval": "hourly"}
    r = requests.get(url, params=params, timeout=20)
    if r.status_code == 429:
        time.sleep(60)
        r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    prices = r.json()["prices"]
    closes = [p[1] for p in prices]
    return closes, closes[-1]

# ── Bybit: balance y órdenes ───────────────────────────────────────────────────
def bybit_headers(body_str=""):
    timestamp   = str(int(time.time() * 1000))
    recv_window = "5000"
    param_str   = timestamp + BYBIT_API_KEY + recv_window + body_str
    signature   = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY":     BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP":   timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN":        signature,
        "Content-Type":       "application/json"
    }

def get_balance(coin):
    url    = f"{BASE_URL}/v5/account/wallet-balance"
    params = {"accountType": "UNIFIED"}
    ts     = str(int(time.time() * 1000))
    rw     = "5000"
    ps     = ts + BYBIT_API_KEY + rw + "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    sig    = hmac.new(BYBIT_API_SECRET.encode(), ps.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": rw,
        "X-BAPI-SIGN": sig
    }
    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()
    for c in r.json()["result"]["list"][0]["coin"]:
        if c["coin"] == coin:
            return float(c["availableToWithdraw"])
    return 0.0

def place_order(side, qty):
    url  = f"{BASE_URL}/v5/order/create"
    body = {"category": "spot", "symbol": SYMBOL, "side": side, "orderType": "Market", "qty": str(round(qty, 3))}
    body_str = json.dumps(body)
    r = requests.post(url, headers=bybit_headers(body_str), data=body_str, timeout=10)
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
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)

# ── Estado ─────────────────────────────────────────────────────────────────────
in_trade   = False
buy_price  = None
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
            closes, price = get_coingecko_data()
            rsi = calculate_rsi(closes)
            now = datetime.now().strftime("%d/%m/%Y %H:%M")

            print(f"[{datetime.now()}] SOL ${price:.2f} | RSI: {rsi} | En trade: {in_trade}")

            if not in_trade:
                if rsi < 35:
                    usdt_balance = get_balance("USDT")
                    usdt_to_use  = usdt_balance * 0.90
                    qty          = usdt_to_use / price

                    if usdt_to_use < 1:
                        send_telegram("⚠️ Balance USDT insuficiente.")
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
                    sol_balance = get_balance(COIN)
                    place_order("Sell", sol_balance)
                    ganancia = sol_balance * (price - buy_price)
                    send_telegram(
                        f"💰 <b>VENTA EJECUTADA — GANANCIA</b>\n\n"
                        f"📈 Entrada: <b>${buy_price:,.2f}</b>\n"
                        f"📈 Salida:  <b>${price:,.2f}</b>\n"
                        f"✅ Ganancia: <b>+{change_pct:.2f}%</b>\n"
                        f"💵 <b>+${ganancia:.2f} USDT</b>\n"
                        f"🕐 {now}"
                    )
                    in_trade = False; buy_price = None; qty_bought = None

                elif change_pct <= -STOP_LOSS_PCT:
                    sol_balance = get_balance(COIN)
                    place_order("Sell", sol_balance)
                    perdida = sol_balance * (buy_price - price)
                    send_telegram(
                        f"🛑 <b>VENTA EJECUTADA — STOP LOSS</b>\n\n"
                        f"📉 Entrada: <b>${buy_price:,.2f}</b>\n"
                        f"📉 Salida:  <b>${price:,.2f}</b>\n"
                        f"❌ Pérdida: <b>{change_pct:.2f}%</b>\n"
                        f"💵 <b>-${perdida:.2f} USDT</b>\n"
                        f"🕐 {now}"
                    )
                    in_trade = False; buy_price = None; qty_bought = None
                else:
                    print(f"  └─ En posición: {change_pct:+.2f}% desde ${buy_price:.2f}")

        except Exception as e:
            print(f"[ERROR] {e}")
            send_telegram(f"⚠️ Error: {e}")

        time.sleep(CHECK_INTERVAL + 10)

if __name__ == "__main__":
    main()
