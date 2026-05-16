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

# ── Monedas a analizar ─────────────────────────────────────────────────────────
COINS = {
    "solana":      {"symbol": "SOLUSDT",  "coin": "SOL"},
    "ethereum":    {"symbol": "ETHUSDT",  "coin": "ETH"},
    "ripple":      {"symbol": "XRPUSDT",  "coin": "XRP"},
    "avalanche-2": {"symbol": "AVAXUSDT", "coin": "AVAX"},
    "chainlink":   {"symbol": "LINKUSDT", "coin": "LINK"},
}

BASE_URL = "https://api.bybit.com"

# ── CoinGecko ──────────────────────────────────────────────────────────────────
def get_coingecko_data(coin_id):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": "3", "interval": "hourly"}
    r = requests.get(url, params=params, timeout=20)
    if r.status_code == 429:
        time.sleep(60)
        r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data   = r.json()
    prices  = [p[1] for p in data["prices"]]
    volumes = [v[1] for v in data["total_volumes"]]
    return prices, volumes

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

def calculate_ema(closes, period):
    k = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_macd(closes):
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    macd  = ema12 - ema26
    return round(macd, 6)

def calculate_bollinger(closes, period=20):
    if len(closes) < period:
        return None, None, None
    subset = closes[-period:]
    mean   = sum(subset) / period
    std    = (sum((x - mean) ** 2 for x in subset) / period) ** 0.5
    upper  = mean + 2 * std
    lower  = mean - 2 * std
    return round(upper, 4), round(mean, 4), round(lower, 4)

def volume_increasing(volumes):
    if len(volumes) < 5:
        return False
    avg_prev = sum(volumes[-6:-1]) / 5
    return volumes[-1] > avg_prev * 1.1

# ── Score de señal (0 a 100) ───────────────────────────────────────────────────
def calculate_score(closes, volumes):
    score  = 0
    price  = closes[-1]
    rsi    = calculate_rsi(closes)
    macd   = calculate_macd(closes)
    upper, mean, lower = calculate_bollinger(closes)
    vol_up = volume_increasing(volumes)

    # RSI (max 35 puntos)
    if rsi < 25:
        score += 35
    elif rsi < 30:
        score += 25
    elif rsi < 35:
        score += 15

    # MACD positivo (max 25 puntos)
    if macd > 0:
        score += 25
    elif macd > -0.001:
        score += 10

    # Bollinger (max 25 puntos)
    if lower and price <= lower:
        score += 25
    elif lower and price <= lower * 1.01:
        score += 15

    # Volumen creciente (max 15 puntos)
    if vol_up:
        score += 15

    return score, rsi, macd, upper, lower, price

# ── Bybit ──────────────────────────────────────────────────────────────────────
def bybit_get_headers(params_str):
    ts  = str(int(time.time() * 1000))
    rw  = "5000"
    ps  = ts + BYBIT_API_KEY + rw + params_str
    sig = hmac.new(BYBIT_API_SECRET.encode(), ps.encode(), hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN": sig, "Content-Type": "application/json"}

def get_balance(coin, retries=3):
    url    = f"{BASE_URL}/v5/account/wallet-balance"
    params = {"accountType": "UNIFIED"}
    ps     = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=bybit_get_headers(ps), timeout=10)
            r.raise_for_status()
            for c in r.json()["result"]["list"][0]["coin"]:
                if c["coin"] == coin:
                    return float(c["availableToWithdraw"])
            return 0.0
        except Exception as e:
            print(f"[BYBIT] get_balance intento {attempt+1}: {e}")
            time.sleep(30)
    return 0.0

def place_order(symbol, side, qty, usdt_amount=None):
    url = f"{BASE_URL}/v5/order/create"
    # Para BUY usamos marketUnit=quoteCoin (monto en USDT)
    # Para SELL usamos qty en tokens
    if side == "Buy" and usdt_amount:
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(round(usdt_amount, 2)),
            "marketUnit": "quoteCoin"
        }
    else:
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(round(qty, 4)),
            "marketUnit": "baseCoin"
        }
    body_str = json.dumps(body)
    r = requests.post(url, headers=bybit_get_headers(body_str), data=body_str, timeout=10)
    r.raise_for_status()
    result = r.json()
    if result["retCode"] != 0:
        raise Exception(f"Bybit: {result['retMsg']}")
    return result

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)

# ── Estado ─────────────────────────────────────────────────────────────────────
in_trade    = False
trade_coin  = None   # coin_id de CoinGecko
trade_info  = None   # {symbol, coin, buy_price, qty}

def main():
    global in_trade, trade_coin, trade_info

    print(f"[{datetime.now()}] Smart Multi Bot iniciado.")
    send_telegram(
        "🧠 <b>Smart Multi Bot activo</b>\n"
        "📊 Analizando: SOL | ETH | XRP | AVAX | LINK\n"
        "🔬 Indicadores: RSI + MACD + Bollinger + Volumen\n"
        f"🎯 Take profit: +{TAKE_PROFIT_PCT}% | 🛑 Stop loss: -{STOP_LOSS_PCT}%\n"
        "⚡ Ejecución automática en Bybit"
    )

    while True:
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        if not in_trade:
            # ── Analizar todas las monedas y elegir la mejor ───────────────
            best_score   = 0
            best_coin_id = None
            best_data    = {}

            for coin_id, info in COINS.items():
                try:
                    closes, volumes = get_coingecko_data(coin_id)
                    score, rsi, macd, upper, lower, price = calculate_score(closes, volumes)
                    print(f"[{datetime.now()}] {info['coin']} Score:{score} RSI:{rsi} MACD:{macd:.4f} Precio:${price:.4f}")

                    if score > best_score:
                        best_score   = score
                        best_coin_id = coin_id
                        best_data    = {"info": info, "closes": closes, "rsi": rsi,
                                       "macd": macd, "lower": lower, "price": price, "score": score}

                    time.sleep(35)  # respetar rate limit CoinGecko
                except Exception as e:
                    print(f"[ERROR] {info['coin']}: {e}")
                    time.sleep(35)

            # ── Ejecutar si el score supera el umbral mínimo (40/100) ──────
            if best_score >= 60 and best_coin_id:
                info  = best_data["info"]
                price = best_data["price"]
                rsi   = best_data["rsi"]
                macd  = best_data["macd"]
                score = best_data["score"]

                # Monto fijo por operación (evita consultar balance que Bybit bloquea)
                usdt_to_use = float(os.getenv("USDT_PER_TRADE", "25"))
                qty         = round(usdt_to_use / price, 2)
                if qty > 0:
                    # Validar que el valor mínimo sea al menos $10
                    if usdt_to_use < 10:
                        print(f"[SKIP] Valor muy bajo: ${usdt_to_use}")
                    else:
                        place_order(info["symbol"], "Buy", qty, usdt_amount=usdt_to_use)
                    in_trade   = True
                    trade_coin = best_coin_id
                    trade_info = {"symbol": info["symbol"], "coin": info["coin"],
                                  "buy_price": price, "qty": qty}

                    send_telegram(
                        f"🟢 <b>COMPRA EJECUTADA — {info['coin']}</b>\n\n"
                        f"🏆 Score de señal: <b>{score}/100</b>\n"
                        f"💰 Precio: <b>${price:,.4f}</b>\n"
                        f"📦 Cantidad: <b>{qty:.4f} {info['coin']}</b>\n"
                        f"💵 Invertido: <b>${usdt_to_use:.2f} USDT</b>\n"
                        f"📊 RSI: <b>{rsi}</b> | MACD: <b>{macd:.4f}</b>\n"
                        f"🎯 Take profit: <b>${price * (1 + TAKE_PROFIT_PCT/100):,.4f}</b>\n"
                        f"🛑 Stop loss:   <b>${price * (1 - STOP_LOSS_PCT/100):,.4f}</b>\n"
                        f"🕐 {now}"
                    )
            else:
                print(f"[{datetime.now()}] Mejor score: {best_score}/100 — sin señal suficiente.")

        else:
            # ── Monitorear posición abierta ────────────────────────────────
            try:
                closes, _ = get_coingecko_data(trade_coin)
                price      = closes[-1]
                buy_price  = trade_info["buy_price"]
                change_pct = ((price - buy_price) / buy_price) * 100
                coin_name  = trade_info["coin"]
                symbol     = trade_info["symbol"]

                print(f"[{datetime.now()}] {coin_name} en posición: {change_pct:+.2f}% desde ${buy_price:.4f}")

                if change_pct >= TAKE_PROFIT_PCT:
                    coin_balance = get_balance(trade_info["coin"])
                    place_order(symbol, "Sell", coin_balance)
                    ganancia = coin_balance * (price - buy_price)
                    send_telegram(
                        f"💰 <b>VENTA EJECUTADA — GANANCIA {coin_name}</b>\n\n"
                        f"📈 Entrada: <b>${buy_price:,.4f}</b>\n"
                        f"📈 Salida:  <b>${price:,.4f}</b>\n"
                        f"✅ Ganancia: <b>+{change_pct:.2f}%</b>\n"
                        f"💵 <b>+${ganancia:.2f} USDT</b>\n"
                        f"🕐 {now}\n\n"
                        f"🔍 Buscando próxima oportunidad..."
                    )
                    in_trade = False; trade_coin = None; trade_info = None

                elif change_pct <= -STOP_LOSS_PCT:
                    coin_balance = get_balance(trade_info["coin"])
                    place_order(symbol, "Sell", coin_balance)
                    perdida = coin_balance * (buy_price - price)
                    send_telegram(
                        f"🛑 <b>VENTA EJECUTADA — STOP LOSS {coin_name}</b>\n\n"
                        f"📉 Entrada: <b>${buy_price:,.4f}</b>\n"
                        f"📉 Salida:  <b>${price:,.4f}</b>\n"
                        f"❌ Pérdida: <b>{change_pct:.2f}%</b>\n"
                        f"💵 <b>-${perdida:.2f} USDT</b>\n"
                        f"🕐 {now}\n\n"
                        f"🔍 Buscando próxima oportunidad..."
                    )
                    in_trade = False; trade_coin = None; trade_info = None

            except Exception as e:
                print(f"[ERROR] monitoreando posición: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
