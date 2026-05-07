import os
import time
import requests
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL_MINUTES", "5")) * 60

TAKE_PROFIT_PCT = 1.5
STOP_LOSS_PCT   = 5.0

# ── CoinGecko: UNA sola llamada por ciclo ──────────────────────────────────────
def get_data():
    url = "https://api.coingecko.com/api/v3/coins/solana/market_chart"
    params = {"vs_currency": "usd", "days": "2", "interval": "hourly"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    prices = r.json()["prices"]
    closes = [p[1] for p in prices]
    return closes, closes[-1]  # historial + precio actual

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
buy_price = None
in_trade  = False

def main():
    global buy_price, in_trade

    print(f"[{datetime.now()}] Bot iniciado.")
    send_telegram(
        "🤖 <b>SOL Bot activo</b>\n"
        f"Take profit: +{TAKE_PROFIT_PCT}% | Stop loss: -{STOP_LOSS_PCT}%\n"
        "Monitoreando cada 5 minutos."
    )

    while True:
        try:
            closes, price = get_data()
            rsi = calculate_rsi(closes)
            now = datetime.now().strftime("%d/%m/%Y %H:%M")

            print(f"[{datetime.now()}] Precio: ${price:.2f} | RSI: {rsi} | En trade: {in_trade}")

            if not in_trade:
                if rsi < 35:
                    buy_price = price
                    in_trade  = True
                    send_telegram(
                        f"🟢 <b>SOL/USDT — COMPRAR</b>\n\n"
                        f"💰 Precio entrada: <b>${price:,.2f}</b>\n"
                        f"📊 RSI: <b>{rsi}</b> (sobrevendido)\n"
                        f"🎯 Take profit: <b>${price * (1 + TAKE_PROFIT_PCT/100):,.2f}</b> (+{TAKE_PROFIT_PCT}%)\n"
                        f"🛑 Stop loss:   <b>${price * (1 - STOP_LOSS_PCT/100):,.2f}</b> (-{STOP_LOSS_PCT}%)\n"
                        f"🕐 {now}"
                    )
            else:
                change_pct = ((price - buy_price) / buy_price) * 100

                if change_pct >= TAKE_PROFIT_PCT:
                    send_telegram(
                        f"💰 <b>SOL/USDT — VENDER (GANANCIA)</b>\n\n"
                        f"📈 Entrada: <b>${buy_price:,.2f}</b>\n"
                        f"📈 Salida:  <b>${price:,.2f}</b>\n"
                        f"✅ Ganancia: <b>+{change_pct:.2f}%</b>\n"
                        f"🕐 {now}"
                    )
                    in_trade  = False
                    buy_price = None

                elif change_pct <= -STOP_LOSS_PCT:
                    send_telegram(
                        f"🛑 <b>SOL/USDT — VENDER (STOP LOSS)</b>\n\n"
                        f"📉 Entrada: <b>${buy_price:,.2f}</b>\n"
                        f"📉 Salida:  <b>${price:,.2f}</b>\n"
                        f"❌ Pérdida: <b>{change_pct:.2f}%</b>\n"
                        f"🕐 {now}"
                    )
                    in_trade  = False
                    buy_price = None

                else:
                    print(f"  └─ En posición: {change_pct:+.2f}% desde ${buy_price:.2f}")

        except Exception as e:
            print(f"[ERROR] {e}")
            send_telegram(f"⚠️ Error en el bot: {e}")

        # Espera 10 segundos extra para no saturar CoinGecko
        time.sleep(CHECK_INTERVAL + 10)

if __name__ == "__main__":
    main()
