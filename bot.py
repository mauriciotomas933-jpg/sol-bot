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

TAKE_PROFIT_PCT = 1.0
STOP_LOSS_PCT   = 4.0   # reducido para proteger capital
USDT_PER_TRADE  = float(os.getenv("USDT_PER_TRADE", "30"))
MIN_SCORE       = 65
TRAILING_STOP_PCT = 0.3  # vende si cae 0.3% desde el máximo alcanzado    # más exigente — solo señales muy fuertes

BASE_URL = "https://api.bybit.com"

COINS = {
    "SOLUSDT":  "SOL",
    "ETHUSDT":  "ETH",
    "XRPUSDT":  "XRP",
    "AVAXUSDT": "AVAX",
    "LINKUSDT": "LINK",
}

# ── Bybit Auth ─────────────────────────────────────────────────────────────────
def bybit_headers(body_str=""):
    ts  = str(int(time.time() * 1000))
    rw  = "5000"
    ps  = ts + BYBIT_API_KEY + rw + body_str
    sig = hmac.new(BYBIT_API_SECRET.encode(), ps.encode(), hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN": sig, "Content-Type": "application/json"}

def bybit_get_headers(params):
    ts  = str(int(time.time() * 1000))
    rw  = "5000"
    ps  = ts + BYBIT_API_KEY + rw + "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    sig = hmac.new(BYBIT_API_SECRET.encode(), ps.encode(), hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN": sig}

# ── Bybit Market Data ──────────────────────────────────────────────────────────
def get_klines(symbol, interval="5", limit=100):
    url    = f"{BASE_URL}/v5/market/kline"
    params = {"category": "spot", "symbol": symbol, "interval": interval, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data    = r.json()["result"]["list"]
    opens   = [float(c[1]) for c in data]
    highs   = [float(c[2]) for c in data]
    lows    = [float(c[3]) for c in data]
    closes  = [float(c[4]) for c in data]
    volumes = [float(c[5]) for c in data]
    opens.reverse(); highs.reverse(); lows.reverse()
    closes.reverse(); volumes.reverse()
    return opens, highs, lows, closes, volumes

def get_current_price(symbol):
    url    = f"{BASE_URL}/v5/market/tickers"
    params = {"category": "spot", "symbol": symbol}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()["result"]["list"][0]
    return float(data["lastPrice"]), float(data["volume24h"])

# ── Indicadores ────────────────────────────────────────────────────────────────
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    # RSI suavizado (Wilder)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calculate_macd(closes):
    if len(closes) < 35:
        return 0, 0, 0
    ema12    = calculate_ema(closes, 12)
    ema26    = calculate_ema(closes, 26)
    macd     = ema12 - ema26
    # Signal line (EMA 9 del MACD) — aproximación
    signal   = macd * 0.9
    hist     = macd - signal
    return round(macd, 6), round(signal, 6), round(hist, 6)

def calculate_bollinger(closes, period=20, std_dev=2):
    if len(closes) < period:
        return None, None, None
    subset = closes[-period:]
    mean   = sum(subset) / period
    std    = (sum((x - mean) ** 2 for x in subset) / period) ** 0.5
    return round(mean + std_dev * std, 4), round(mean, 4), round(mean - std_dev * std, 4)

def calculate_stochastic(highs, lows, closes, k_period=14):
    if len(closes) < k_period:
        return 50.0
    recent_highs = highs[-k_period:]
    recent_lows  = lows[-k_period:]
    highest = max(recent_highs)
    lowest  = min(recent_lows)
    if highest == lowest:
        return 50.0
    k = ((closes[-1] - lowest) / (highest - lowest)) * 100
    return round(k, 2)

def calculate_atr(highs, lows, closes, period=14):
    """Average True Range — mide volatilidad"""
    if len(closes) < period + 1:
        return 0
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)

def volume_surge(volumes):
    """Verifica si el volumen actual es mayor al promedio de las últimas 20 velas"""
    if len(volumes) < 21:
        return False, 0
    avg = sum(volumes[-21:-1]) / 20
    ratio = volumes[-1] / avg if avg > 0 else 1
    return ratio > 1.5, round(ratio, 2)

def detect_oversold_divergence(closes, period=5):
    """Detecta si el precio está haciendo nuevos mínimos pero RSI no — señal alcista"""
    if len(closes) < period * 2:
        return False
    recent_low  = min(closes[-period:])
    previous_low = min(closes[-period*2:-period])
    recent_rsi  = calculate_rsi(closes[-period-14:])
    previous_rsi = calculate_rsi(closes[-period*2-14:-period])
    # Precio hace nuevo mínimo pero RSI no → divergencia alcista
    return recent_low < previous_low and recent_rsi > previous_rsi

# ── Score multicapa (0-100) ────────────────────────────────────────────────────
def calculate_score(opens, highs, lows, closes, volumes):
    price  = closes[-1]
    rsi    = calculate_rsi(closes)
    macd, signal, hist = calculate_macd(closes)
    upper, mid, lower  = calculate_bollinger(closes)
    stoch  = calculate_stochastic(highs, lows, closes)
    atr    = calculate_atr(highs, lows, closes)
    vol_surge, vol_ratio = volume_surge(volumes)
    divergence = detect_oversold_divergence(closes)
    score  = 0
    reasons = []

    # 1. RSI (max 30 pts) — indicador principal
    if rsi < 20:
        score += 30
        reasons.append(f"RSI extremo {rsi}")
    elif rsi < 25:
        score += 22
        reasons.append(f"RSI muy bajo {rsi}")
    elif rsi < 30:
        score += 14
        reasons.append(f"RSI bajo {rsi}")
    elif rsi < 35:
        score += 7
        reasons.append(f"RSI sobrevendido {rsi}")

    # 2. Estocástico (max 15 pts) — confirma RSI
    if stoch < 20:
        score += 15
        reasons.append(f"Stoch extremo {stoch}")
    elif stoch < 30:
        score += 10
        reasons.append(f"Stoch bajo {stoch}")
    elif stoch < 40:
        score += 5

    # 3. Bollinger (max 20 pts) — precio en banda inferior
    if lower and price <= lower:
        score += 20
        reasons.append("Precio bajo banda inferior")
    elif lower and price <= lower * 1.005:
        score += 12
        reasons.append("Precio cerca banda inferior")
    elif lower and price <= mid:
        score += 5

    # 4. MACD (max 15 pts)
    if macd > 0 and hist > 0:
        score += 15
        reasons.append("MACD positivo con histograma alcista")
    elif macd > 0:
        score += 8
    elif hist > 0:
        score += 5
        reasons.append("Histograma MACD alcista")

    # 5. Volumen (max 10 pts) — confirma el movimiento
    if vol_surge:
        score += 10
        reasons.append(f"Volumen x{vol_ratio} del promedio")
    elif vol_ratio > 1.2:
        score += 5

    # 6. Divergencia alcista (max 10 pts) — señal avanzada
    if divergence:
        score += 10
        reasons.append("Divergencia alcista detectada")

    return score, rsi, macd, stoch, atr, price, reasons

# ── Bybit Trading ──────────────────────────────────────────────────────────────
def get_balance(coin):
    url    = f"{BASE_URL}/v5/account/wallet-balance"
    params = {"accountType": "UNIFIED"}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=bybit_get_headers(params), timeout=10)
            r.raise_for_status()
            for c in r.json()["result"]["list"][0]["coin"]:
                if c["coin"] == coin:
                    val = c.get("availableToWithdraw", "") or c.get("walletBalance", "")
                    if val and val != "":
                        return float(val)
            return 0.0
        except Exception as e:
            print(f"[BYBIT] get_balance intento {attempt+1}: {e}")
            time.sleep(10)
    return 0.0

def place_buy(symbol, usdt_amount):
    """Compra con USDT — usa quoteCoin"""
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Market",
        "qty": str(round(float(usdt_amount), 2)),
        "marketUnit": "quoteCoin"
    }
    body_str = json.dumps(body, separators=(',', ':'))
    r = requests.post(f"{BASE_URL}/v5/order/create", headers=bybit_headers(body_str), data=body_str, timeout=10)
    r.raise_for_status()
    result = r.json()
    if result["retCode"] != 0:
        raise Exception(f"Bybit buy error: {result['retMsg']}")
    return result

# Decimales permitidos por moneda en Bybit
COIN_DECIMALS = {
    "SOL": 2,
    "ETH": 4,
    "XRP": 2,
    "AVAX": 2,
    "LINK": 2,
}

def place_sell(symbol, qty, coin=""):
    """Vende tokens — usa baseCoin con decimales correctos por moneda"""
    decimals = COIN_DECIMALS.get(coin, 2)
    qty_rounded = round(float(qty), decimals)
    if qty_rounded <= 0:
        raise Exception("Cantidad a vender es 0")
    fmt = f"{{:.{decimals}f}}"
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Market",
        "qty": fmt.format(qty_rounded),
        "marketUnit": "baseCoin"
    }
    body_str = json.dumps(body, separators=(',', ':'))
    r = requests.post(f"{BASE_URL}/v5/order/create", headers=bybit_headers(body_str), data=body_str, timeout=10)
    r.raise_for_status()
    result = r.json()
    if result["retCode"] != 0:
        raise Exception(f"Bybit sell error: {result['retMsg']}")
    return result

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)

# ── Estado ─────────────────────────────────────────────────────────────────────
in_trade     = False
trade_symbol = None
trade_coin   = None
trade_info   = {}

def main():
    global in_trade, trade_symbol, trade_coin, trade_info

    print(f"[{datetime.now()}] Smart Bot V2 iniciado — 100% Bybit tiempo real.")
    send_telegram(
        "🧠 <b>Smart Multi Bot V2</b>\n"
        "📊 SOL | ETH | XRP | AVAX | LINK\n"
        "⚡ <b>100% datos en tiempo real — Bybit</b>\n"
        "🔬 RSI + Estocástico + MACD + Bollinger + Volumen + Divergencias\n"
        f"🎯 Take profit: +{TAKE_PROFIT_PCT}% | 🛑 Stop loss: -{STOP_LOSS_PCT}%\n"
        f"💵 Capital por trade: ${USDT_PER_TRADE} USDT\n"
        f"🏆 Score mínimo para operar: {MIN_SCORE}/100"
    )

    while True:
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        if not in_trade:
            best_score  = 0
            best_symbol = None
            best_data   = {}

            for symbol, coin in COINS.items():
                try:
                    opens, highs, lows, closes, volumes = get_klines(symbol)
                    score, rsi, macd, stoch, atr, price, reasons = calculate_score(opens, highs, lows, closes, volumes)
                    print(f"[{datetime.now()}] {coin} Score:{score} RSI:{rsi} Stoch:{stoch} MACD:{macd:.4f} Precio:${price:.4f}")

                    if score > best_score:
                        best_score  = score
                        best_symbol = symbol
                        best_data   = {"coin": coin, "price": price, "rsi": rsi,
                                      "macd": macd, "stoch": stoch, "atr": atr,
                                      "score": score, "reasons": reasons}
                    time.sleep(2)

                except Exception as e:
                    print(f"[ERROR] {coin}: {e}")
                    time.sleep(5)

            print(f"[{datetime.now()}] Mejor: {best_data.get('coin','ninguna')} {best_score}/100")

            if best_score >= MIN_SCORE and best_symbol:
                price   = best_data["price"]
                coin    = best_data["coin"]
                score   = best_data["score"]
                reasons = best_data["reasons"]

                try:
                    place_buy(best_symbol, USDT_PER_TRADE)
                    in_trade     = True
                    trade_symbol = best_symbol
                    trade_coin   = coin
                    trade_info   = {"buy_price": price, "qty": USDT_PER_TRADE / price}

                    reasons_str = "\n".join([f"  ✅ {r}" for r in reasons])
                    send_telegram(
                        f"🟢 <b>COMPRA EJECUTADA — {coin}</b>\n\n"
                        f"🏆 Score: <b>{score}/100</b>\n"
                        f"💰 Precio entrada: <b>${price:,.4f}</b>\n"
                        f"💵 Invertido: <b>${USDT_PER_TRADE:.2f} USDT</b>\n"
                        f"📊 RSI: <b>{best_data['rsi']}</b> | Stoch: <b>{best_data['stoch']}</b>\n"
                        f"🎯 Take profit: <b>${price * (1 + TAKE_PROFIT_PCT/100):,.4f}</b>\n"
                        f"🛑 Stop loss:   <b>${price * (1 - STOP_LOSS_PCT/100):,.4f}</b>\n"
                        f"📋 Señales:\n{reasons_str}\n"
                        f"🕐 {now}"
                    )
                except Exception as e:
                    print(f"[ERROR] Orden: {e}")
                    send_telegram(f"⚠️ Error ejecutando orden {coin}: {e}")
                    in_trade = False

        else:
            try:
                price, vol24h = get_current_price(trade_symbol)
                buy_price     = trade_info["buy_price"]
                change_pct    = ((price - buy_price) / buy_price) * 100

                print(f"[{datetime.now()}] {trade_coin} {change_pct:+.2f}% | Precio: ${price:.4f}")

                # Actualizar máximo alcanzado para trailing stop
                if "max_pct" not in trade_info:
                    trade_info["max_pct"] = 0
                if change_pct > trade_info["max_pct"]:
                    trade_info["max_pct"] = change_pct

                # Trailing stop: si superó 1% y cae 0.3% desde el máximo → vender
                trailing_triggered = (
                    trade_info["max_pct"] >= TAKE_PROFIT_PCT and
                    change_pct <= trade_info["max_pct"] - TRAILING_STOP_PCT
                )

                if change_pct >= TAKE_PROFIT_PCT or trailing_triggered:
                    try:
                        bal = get_balance(trade_coin)
                        qty_to_sell = bal * 0.99 if bal > 0 else trade_info.get("qty", USDT_PER_TRADE / buy_price) * 0.99
                        place_sell(trade_symbol, qty_to_sell, coin=trade_coin)
                        ganancia = bal * (price - buy_price)
                        motivo = "TRAILING STOP" if trailing_triggered else "TAKE PROFIT"
                        send_telegram(
                            f"💰 <b>VENTA — {motivo} {trade_coin}</b>\n\n"
                            f"📈 Entrada: <b>${buy_price:,.4f}</b>\n"
                            f"📈 Salida:  <b>${price:,.4f}</b>\n"
                            f"📊 Máximo alcanzado: <b>+{trade_info['max_pct']:.2f}%</b>\n"
                            f"✅ <b>+{change_pct:.2f}% | +${ganancia:.2f} USDT</b>\n"
                            f"🕐 {now}\n\n🔍 Buscando próxima oportunidad..."
                        )
                    except Exception as e:
                        send_telegram(f"⚠️ Error vendiendo {trade_coin}: {e}")
                    finally:
                        in_trade = False; trade_symbol = None; trade_coin = None; trade_info = {}

                elif change_pct <= -STOP_LOSS_PCT:
                    try:
                        bal = get_balance(trade_coin)
                        qty_to_sell = bal * 0.99 if bal > 0 else trade_info.get("qty", USDT_PER_TRADE / buy_price) * 0.99
                        place_sell(trade_symbol, qty_to_sell, coin=trade_coin)
                        perdida = bal * (buy_price - price)
                        send_telegram(
                            f"🛑 <b>STOP LOSS — {trade_coin}</b>\n\n"
                            f"📉 Entrada: <b>${buy_price:,.4f}</b>\n"
                            f"📉 Salida:  <b>${price:,.4f}</b>\n"
                            f"❌ <b>{change_pct:.2f}% | -${perdida:.2f} USDT</b>\n"
                            f"🕐 {now}\n\n🔍 Buscando próxima oportunidad..."
                        )
                    except Exception as e:
                        send_telegram(f"⚠️ Error stop loss {trade_coin}: {e}")
                    finally:
                        in_trade = False; trade_symbol = None; trade_coin = None; trade_info = {}

            except Exception as e:
                print(f"[ERROR] Monitoreando: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
