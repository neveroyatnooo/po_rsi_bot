# main.py

import time
import logging
import threading
from collections import deque

import pandas as pd
from ta.momentum import RSIIndicator
from telegram import Bot
import socketio
import config

# 1) Настроим логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger()

# 2) Телеграм-бот
bot = Bot(token=config.TELEGRAM_TOKEN)

# 3) Socket.IO клиент: только WebSocket-транспорт
sio = socketio.Client(transports=["websocket"])

# 4) Храним последние 1-мин свечи и уже посланные сигналы
hist = {pair: deque(maxlen=100) for pair in config.OTC_PAIRS}
sent_signals = set()

def on_new_candle(pair: str, candle: dict):
    dq = hist[pair]
    dq.append({
        "time":  candle["from"],
        "open":  candle["open"],
        "high":  candle["high"],
        "low":   candle["low"],
        "close": candle["close"],
    })
    if len(dq) < max(config.RSI_PERIODS) + 1:
        return

    df = pd.DataFrame(dq)
    dirs = []
    for p in config.RSI_PERIODS:
        rsi = RSIIndicator(df["close"], window=p).rsi().iloc[-1]
        if rsi > config.RSI_UPPER:
            dirs.append("DOWN")
        elif rsi < config.RSI_LOWER:
            dirs.append("UP")
        else:
            return

    if all(d == dirs[0] for d in dirs):
        text = "Вниз" if dirs[0] == "DOWN" else "Вверх"
        ts   = df["time"].iloc[-1]
        key  = (pair, ts, text)
        if key not in sent_signals:
            sent_signals.add(key)
            msg = f"{pair} | экспирация {config.EXPIRATION_MIN} мин | {text}"
            try:
                bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg)
                logger.info("✅ Signal sent: %s", msg)
            except Exception as e:
                logger.error("Telegram error: %s", e)

# 5) Socket.IO handlers

@sio.event
def connect():
    logger.info("🟢 Connected to %s via WebSocket", config.PO_SOCKET_HOST)
    sio.emit("authenticate", {
        "email":    config.PO_EMAIL,
        "password": config.PO_PASSWORD
    })

@sio.on("auth_response")
def on_auth(data):
    if data.get("status") == "ok":
        logger.info("🔓 Auth OK")
        for pair in config.OTC_PAIRS:
            sio.emit("subscribe_candles", {
                "instrument": pair,
                "timeframe":  60
            })
    else:
        logger.error("❌ Auth failed: %s", data)

@sio.on("candle")
def on_candle(msg):
    instr = msg.get("instrument")
    if instr in config.OTC_PAIRS and msg.get("timeframe") == 60:
        on_new_candle(instr, msg.get("candle", {}))

@sio.event
def disconnect():
    logger.warning("🔴 Disconnected from PO")

# 6) Запуск WebSocket-соединения
def start_ws():
    url = f"https://{config.PO_SOCKET_HOST}"
    sio.connect(
        url,
        transports=["websocket"],
        headers={"Origin": "https://pocketoption.com"},
        socketio_path=config.PO_SOCKET_PATH
    )
    sio.wait()

if __name__ == "__main__":
    threading.Thread(target=start_ws, daemon=True).start()
    logger.info("🤖 Bot is running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down…")
        sio.disconnect()
