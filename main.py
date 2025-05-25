# main.py

import time
import logging
import threading
from collections import deque

import urllib3
import pandas as pd
from ta.momentum import RSIIndicator
from telegram import Bot
import socketio
import requests

import config

# отключаем ворнинги SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1) Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger()

# 2) Инициализируем Telegram-бота
bot = Bot(token=config.TELEGRAM_TOKEN)

# 3) HTTP-сессия для Engine.IO-polling (без проверки SSL)
session = requests.Session()
session.verify = False

# 4) Socket.IO-клиент
sio = socketio.Client(http_session=session)

# 5) История свечей и множество уже отправленных сигналов
hist = {pair: deque(maxlen=100) for pair in config.OTC_PAIRS}
sent_signals = set()

def on_new_candle(pair: str, candle: dict):
    """
    Добавляем новую минутную свечу, считаем RSI(7/14/21),
    если все три >RSI_UPPER → DOWN, <RSI_LOWER → UP.
    """
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
    for period in config.RSI_PERIODS:
        rsi = RSIIndicator(df["close"], window=period).rsi().iloc[-1]
        if rsi > config.RSI_UPPER:
            dirs.append("DOWN")
        elif rsi < config.RSI_LOWER:
            dirs.append("UP")
        else:
            return  # хотя бы один в нейтрали — отменяем

    if all(d == dirs[0] for d in dirs):
        text = "Вниз" if dirs[0] == "DOWN" else "Вверх"
        ts = df["time"].iloc[-1]
        key = (pair, ts, text)
        if key not in sent_signals:
            sent_signals.add(key)
            msg = f"{pair} | экспирация {config.EXPIRATION_MIN} мин | {text}"
            try:
                bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg)
                logger.info("✅ Signal sent: %s", msg)
            except Exception as e:
                logger.error("Telegram error: %s", e)

# --- Socket.IO handlers ---

@sio.event
def connect():
    logger.info("🟢 Connected to %s (polling)", config.PO_SOCKET_HOST)
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

# 7) Старт Engine.IO polling
def start_polling():
    url = f"https://{config.PO_SOCKET_HOST}"
    sio.connect(
        url,
        transports=["polling"],
        headers={"Origin": "https://pocketoption.com"},
        socketio_path="socket.io"
    )
    sio.wait()

if __name__ == "__main__":
    # запускаем в фоне
    threading.Thread(target=start_polling, daemon=True).start()
    logger.info("🤖 Bot is running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down…")
        sio.disconnect()
