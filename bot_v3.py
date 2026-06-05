import os
import time
import hmac
import hashlib
import json
import requests
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BYBIT_API_KEY    = os.environ["BYBIT_API_KEY"]
BYBIT_API_SECRET = os.environ["BYBIT_API_SECRET"]
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL_MINUTES", "5")) * 60

TAKE_PROFIT_BASE  = 1.0   # % base de take profit
STOP_LOSS_PCT     = 4.0   # % de stop loss
USDT_PER_TRADE    = float(os.getenv("USDT_PER_TRADE", "60"))
MIN_SCORE         = 65
TRAILING_STOP_PCT = 0.3

# Horario de operación UTC (mayor volumen cripto)
TRADE_HOURS_START = 13  # 10hs Argentina
TRADE_HOURS_END   = 23  # 20hs Argentina

BASE_URL = "https://api.bybit.com"

COINS = {
    "SOLUSDT":  "SOL",
    "ETHUSDT":  "ETH",
    "XRPUSDT":  "XRP",
    "AVAXUSDT": "AVAX",
    "LINKUSDT": "LINK",
}

COIN_DECIMALS = {
    "SOL": 2, "ETH": 4, "XRP": 2, "AVAX": 2, "LINK": 2,
}

# ── Blacklist temporal ─────────────────────────────────────────────────────────
blacklist = {}  # {symbol: timestamp_hasta_cuando_ignorar}
BLACKLIST_HOURS = 3

def is_blacklisted(symbol):
    if symbol in blacklist:
        if time.time() < blacklist[symbol]:
            return True
        else:
            del blacklist[symbol]
    return False

def add_to_blacklist(symbol):
    blacklist[symbol] = time.time() + BLACKLIST_HOURS * 3600
    print(f"[BLACKLIST] {symbol} bloqueado por {BLACKLIST_HOURS}hs")

# ── Horario de operación ───────────────────────────────────────────────────────
def is_trading_hours():
    hour = datetime.now(timezone.utc).hour
    return TRADE_HOURS_START <= hour <= TRADE_HOURS_END

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

def get_btc_trend():
    """Verifica si BTC está en tendencia bajista — si cae >2% en 1h, mercado en pánico"""
    try:
        _, _, _, closes, _ = get_klines("BTCUSDT", interval="60", limit=3)
        if len(closes) >= 2:
            change = ((closes[-1] - closes[-2]) / closes[-2]) * 100
            return change  # positivo = sube, negativo = baja
    except:
        pass
    return 0

# ── Indicadores ────────────────────────────────────────────────────────────────
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
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
    ema12   = calculate_ema(closes, 12)
    ema26   = calculate_ema(closes, 26)
    macd    = ema12 - ema26
    signal  = macd * 0.9
    hist    = macd - signal
    return round(macd, 6), round(signal, 6), round(hist, 6)

def calculate_bollinger(closes, period=20):
    if len(closes) < period:
        return None, None, None
    subset = closes[-period:]
    mean   = sum(subset) / period
    std    = (sum((x - mean) ** 2 for x in subset) / period) ** 0.5
    return round(mean + 2 * std, 4), round(mean, 4), round(mean - 2 * std, 4)

def calculate_stochastic(highs, lows, closes, k_period=14):
    if len(closes) < k_period:
        return 50.0
    highest = max(highs[-k_period:])
    lowest  = min(lows[-k_period:])
    if highest == lowest:
        return 50.0
    return round(((closes[-1] - lowest) / (highest - lowest)) * 100, 2)

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 6)

def volume_surge(volumes):
    if len(volumes) < 21:
        return False, 0
    avg   = sum(volumes[-21:-1]) / 20
    ratio = volumes[-1] / avg if avg > 0 else 1
    return ratio > 1.5, round(ratio, 2)

def detect_divergence(closes, period=5):
    if len(closes) < period * 2:
        return False
    recent_low   = min(closes[-period:])
    previous_low = min(closes[-period*2:-period])
    recent_rsi   = calculate_rsi(closes[-period-14:])
    previous_rsi = calculate_rsi(closes[-period*2-14:-period])
    return recent_low < previous_low and recent_rsi > previous_rsi

# ── Take profit dinámico por volatilidad ──────────────────────────────────────
def dynamic_take_profit(atr, price):
    atr_pct = (atr / price) * 100
    if atr_pct > 0.5:
        return 1.5  # mercado volátil → apuntar más alto
    elif atr_pct < 0.2:
        return 0.8  # mercado tranquilo → salir antes
    return TAKE_PROFIT_BASE

# ── Tendencia individual ──────────────────────────────────────────────────────
def is_downtrend(closes, candles=3):
    """Verifica si la moneda cayó las últimas N velas seguidas"""
    if len(closes) < candles + 1:
        return False
    for i in range(1, candles + 1):
        if closes[-i] >= closes[-(i+1)]:
            return False
    return True

# ── Score ──────────────────────────────────────────────────────────────────────
def calculate_score(opens, highs, lows, closes, volumes):
    price  = closes[-1]
    rsi    = calculate_rsi(closes)
    macd, signal, hist = calculate_macd(closes)
    upper, mid, lower  = calculate_bollinger(closes)
    stoch  = calculate_stochastic(highs, lows, closes)
    atr    = calculate_atr(highs, lows, closes)
    vol_surge_flag, vol_ratio = volume_surge(volumes)
    divergence = detect_divergence(closes)
    score   = 0
    reasons = []

    if rsi < 20:
        score += 30; reasons.append(f"RSI extremo {rsi}")
    elif rsi < 25:
        score += 22; reasons.append(f"RSI muy bajo {rsi}")
    elif rsi < 30:
        score += 14; reasons.append(f"RSI bajo {rsi}")
    elif rsi < 35:
        score += 7;  reasons.append(f"RSI sobrevendido {rsi}")

    if stoch < 20:
        score += 15; reasons.append(f"Stoch extremo {stoch}")
    elif stoch < 30:
        score += 10; reasons.append(f"Stoch bajo {stoch}")
    elif stoch < 40:
        score += 5

    if lower and price <= lower:
        score += 20; reasons.append("Precio bajo banda inferior")
    elif lower and price <= lower * 1.005:
        score += 12; reasons.append("Precio cerca banda inferior")
    elif lower and price <= mid:
        score += 5

    if macd > 0 and hist > 0:
        score += 15; reasons.append("MACD positivo con histograma alcista")
    elif macd > 0:
        score += 8
    elif hist > 0:
        score += 5; reasons.append("Histograma MACD alcista")

    if vol_surge_flag:
        score += 10; reasons.append(f"Volumen x{vol_ratio} del promedio")
    elif vol_ratio > 1.2:
        score += 5

    if divergence:
        score += 10; reasons.append("Divergencia alcista detectada")

    tp = dynamic_take_profit(atr, price)
    return score, rsi, macd, stoch, atr, price, reasons, tp

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
    body = {
        "category": "spot", "symbol": symbol, "side": "Buy",
        "orderType": "Market", "qty": str(round(float(usdt_amount), 2)),
        "marketUnit": "quoteCoin"
    }
    body_str = json.dumps(body, separators=(',', ':'))
    r = requests.post(f"{BASE_URL}/v5/order/create", headers=bybit_headers(body_str), data=body_str, timeout=10)
    r.raise_for_status()
    result = r.json()
    if result["retCode"] != 0:
        raise Exception(f"Bybit buy error: {result['retMsg']}")
    return result

def place_sell(symbol, qty, coin=""):
    decimals = COIN_DECIMALS.get(coin, 2)
    fmt = f"{{:.{decimals}f}}"
    for factor in [1.0, 0.98]:
        qty_try = round(float(qty) * factor, decimals)
        if qty_try <= 0:
            continue
        body = {
            "category": "spot", "symbol": symbol, "side": "Sell",
            "orderType": "Market", "qty": fmt.format(qty_try),
            "marketUnit": "baseCoin"
        }
        body_str = json.dumps(body, separators=(',', ':'))
        r = requests.post(f"{BASE_URL}/v5/order/create", headers=bybit_headers(body_str), data=body_str, timeout=10)
        r.raise_for_status()
        result = r.json()
        if result["retCode"] == 0:
            return result
    raise Exception(f"Bybit sell error: {result['retMsg']}")

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

    print(f"[{datetime.now()}] Smart Bot V3 iniciado.")
    send_telegram(
        "🧠 <b>Smart Multi Bot V3</b>\n"
        "📊 SOL | ETH | XRP | AVAX | LINK\n"
        "⚡ 100% datos en tiempo real — Bybit\n"
        "🔬 RSI + Stoch + MACD + Bollinger + Vol + Divergencias\n"
        "🆕 Filtro BTC + Horario óptimo + Take profit dinámico + Blacklist\n"
        f"🎯 Take profit dinámico | 🛑 Stop loss: -{STOP_LOSS_PCT}%\n"
        f"💵 Capital por trade: ${USDT_PER_TRADE} USDT\n"
        f"🏆 Score mínimo: {MIN_SCORE}/100"
    )

    while True:
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        if not in_trade:
            # ── Verificar horario ──────────────────────────────────────────
            if not is_trading_hours():
                hour_utc = datetime.now(timezone.utc).hour
                print(f"[{datetime.now()}] Fuera de horario óptimo (UTC {hour_utc}h). Esperando...")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Verificar tendencia de BTC ────────────────────────────────
            btc_change = get_btc_trend()
            if btc_change <= -2.0:
                print(f"[{datetime.now()}] BTC cayendo {btc_change:.2f}% — mercado en pánico, no operar.")
                time.sleep(CHECK_INTERVAL)
                continue

            best_score  = 0
            best_symbol = None
            best_data   = {}

            for symbol, coin in COINS.items():
                if is_blacklisted(symbol):
                    print(f"[{datetime.now()}] {coin} en blacklist, saltando.")
                    continue
                try:
                    opens, highs, lows, closes, volumes = get_klines(symbol)

                    # Filtro de tendencia individual — si cayó 3 velas seguidas, saltar
                    if is_downtrend(closes, candles=3):
                        print(f"[{datetime.now()}] {coin} en tendencia bajista (3 velas rojas), saltando.")
                        time.sleep(2)
                        continue

                    score, rsi, macd, stoch, atr, price, reasons, tp = calculate_score(opens, highs, lows, closes, volumes)
                    print(f"[{datetime.now()}] {coin} Score:{score} RSI:{rsi} Stoch:{stoch} TP:{tp}% Precio:${price:.4f}")

                    if score > best_score:
                        best_score  = score
                        best_symbol = symbol
                        best_data   = {"coin": coin, "price": price, "rsi": rsi,
                                      "macd": macd, "stoch": stoch, "atr": atr,
                                      "score": score, "reasons": reasons, "tp": tp}
                    time.sleep(2)
                except Exception as e:
                    print(f"[ERROR] {coin}: {e}")
                    time.sleep(5)

            print(f"[{datetime.now()}] BTC: {btc_change:+.2f}% | Mejor: {best_data.get('coin','ninguna')} {best_score}/100")

            if best_score >= MIN_SCORE and best_symbol:
                price   = best_data["price"]
                coin    = best_data["coin"]
                score   = best_data["score"]
                reasons = best_data["reasons"]
                tp      = best_data["tp"]

                try:
                    place_buy(best_symbol, USDT_PER_TRADE)
                    in_trade     = True
                    trade_symbol = best_symbol
                    trade_coin   = coin
                    trade_info   = {"buy_price": price, "qty": USDT_PER_TRADE / price,
                                   "max_pct": 0, "tp": tp}

                    reasons_str = "\n".join([f"  ✅ {r}" for r in reasons])
                    send_telegram(
                        f"🟢 <b>COMPRA EJECUTADA — {coin}</b>\n\n"
                        f"🏆 Score: <b>{score}/100</b>\n"
                        f"💰 Precio entrada: <b>${price:,.4f}</b>\n"
                        f"💵 Invertido: <b>${USDT_PER_TRADE:.2f} USDT</b>\n"
                        f"📊 RSI: <b>{best_data['rsi']}</b> | Stoch: <b>{best_data['stoch']}</b>\n"
                        f"📈 BTC tendencia: <b>{btc_change:+.2f}%</b>\n"
                        f"🎯 Take profit dinámico: <b>${price * (1 + tp/100):,.4f}</b> (+{tp}%)\n"
                        f"🛑 Stop loss: <b>${price * (1 - STOP_LOSS_PCT/100):,.4f}</b>\n"
                        f"📋 Señales:\n{reasons_str}\n"
                        f"🕐 {now}"
                    )
                except Exception as e:
                    print(f"[ERROR] Orden: {e}")
                    send_telegram(f"⚠️ Error ejecutando orden {coin}: {e}")
                    in_trade = False

        else:
            try:
                price, _   = get_current_price(trade_symbol)
                buy_price  = trade_info["buy_price"]
                change_pct = ((price - buy_price) / buy_price) * 100
                tp         = trade_info.get("tp", TAKE_PROFIT_BASE)

                if change_pct > trade_info["max_pct"]:
                    trade_info["max_pct"] = change_pct

                trailing_triggered = (
                    trade_info["max_pct"] >= tp and
                    change_pct <= trade_info["max_pct"] - TRAILING_STOP_PCT
                )

                print(f"[{datetime.now()}] {trade_coin} {change_pct:+.2f}% (max:{trade_info['max_pct']:+.2f}%) TP:{tp}%")

                if change_pct >= tp or trailing_triggered:
                    try:
                        bal = get_balance(trade_coin)
                        qty = bal if bal > 0 else trade_info["qty"]
                        place_sell(trade_symbol, qty, coin=trade_coin)
                        ganancia = qty * (price - buy_price)
                        motivo = "TRAILING STOP" if trailing_triggered else "TAKE PROFIT"
                        send_telegram(
                            f"💰 <b>VENTA — {motivo} {trade_coin}</b>\n\n"
                            f"📈 Entrada: <b>${buy_price:,.4f}</b>\n"
                            f"📈 Salida:  <b>${price:,.4f}</b>\n"
                            f"📊 Máximo: <b>+{trade_info['max_pct']:.2f}%</b>\n"
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
                        qty = bal if bal > 0 else trade_info["qty"]
                        place_sell(trade_symbol, qty, coin=trade_coin)
                        perdida = qty * (buy_price - price)
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
                        add_to_blacklist(trade_symbol)
                        in_trade = False; trade_symbol = None; trade_coin = None; trade_info = {}

            except Exception as e:
                print(f"[ERROR] Monitoreando: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
