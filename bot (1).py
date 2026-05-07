import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime

# ── Config desde variables de entorno ──────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "5")) * 60

# ── Binance: precio SOL/USDT ───────────────────────────────────────────────────
SYMBOL = "SOLUSDT"
INTERVAL = "5m"    # velas de 5 minutos
LIMIT = 100        # últimas 100 velas (suficiente para MA50 + RSI)

def get_klines():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    closes = [float(c[4]) for c in data]  # índice 4 = precio de cierre
    return closes

# ── Indicadores ────────────────────────────────────────────────────────────────
def calculate_rsi(closes, period=14):
    s = pd.Series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 2)

def calculate_ma(closes, period=50):
    return round(np.mean(closes[-period:]), 4)

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    requests.post(url, json=payload, timeout=10)

# ── Lógica de señal ────────────────────────────────────────────────────────────
def get_signal(price, rsi, ma50):
    if rsi < 35 and price > ma50:
        return "BUY"
    elif rsi > 65 and price < ma50:
        return "SELL"
    return "HOLD"

SIGNAL_EMOJIS = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}
SIGNAL_LABELS = {"BUY": "COMPRAR", "SELL": "VENDER", "HOLD": "MANTENER"}

# ── Loop principal ─────────────────────────────────────────────────────────────
last_signal = None

def main():
    global last_signal
    print(f"[{datetime.now()}] Bot iniciado. Chequeando cada {CHECK_INTERVAL//60} minutos.")
    send_telegram("🤖 <b>SOL Bot activo</b>\nMonitoreando Solana cada 15 minutos.")

    while True:
        try:
            closes = get_klines()
            price = closes[-1]
            rsi = calculate_rsi(closes)
            ma50 = calculate_ma(closes, 50)
            signal = get_signal(price, rsi, ma50)

            print(f"[{datetime.now()}] Precio: ${price} | RSI: {rsi} | MA50: ${ma50} | Señal: {signal}")

            # Solo notifica si la señal cambió (evita spam)
            if signal != last_signal:
                emoji = SIGNAL_EMOJIS[signal]
                label = SIGNAL_LABELS[signal]
                msg = (
                    f"{emoji} <b>SOL/USDT — {label}</b>\n\n"
                    f"💰 Precio: <b>${price:,.2f}</b>\n"
                    f"📊 RSI (14): <b>{rsi}</b>\n"
                    f"📈 MA50: <b>${ma50:,.2f}</b>\n"
                    f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                )
                if signal == "BUY":
                    msg += "📌 <i>RSI sobrevendido + precio sobre MA50. Señal de entrada.</i>"
                elif signal == "SELL":
                    msg += "📌 <i>RSI sobrecomprado + precio bajo MA50. Señal de salida.</i>"
                else:
                    msg += "📌 <i>Sin señal clara. Esperar.</i>"

                send_telegram(msg)
                last_signal = signal

        except Exception as e:
            print(f"[ERROR] {e}")
            send_telegram(f"⚠️ Error en el bot: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
