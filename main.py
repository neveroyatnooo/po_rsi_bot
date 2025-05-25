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

# 0) Отключаем ворнинги по сбросу проверки SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1) Настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger()

# 2) Инициализируем Telegram-бота
bot = Bot(token=config.TELEGRAM_TOKEN)

# 3) Готовим HTTP-сессию для Engine.IO через SOCKS5
session = requests.Session()
session.verify = False
if config.USE_PROXY:
    proxy_url = f"socks5h://{config.PROXY_ADDR}:{config.PROXY_PORT}"
    session.proxies.update({
        "http":  proxy_url,
        "https": proxy_url
    })
    logger.info(f"HTTP(S) через прокси {proxy_url}")

# 4) Создаём Socket.IO-клиент, передаём ему нашу session
sio = socketio.Client(http_session=session)

# 5) Стратегия: история свечей и фильтр по RSI
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
    # Ждём хотя бы max_period+1 точки
    if len(dq) < max(config.RSI_PERIODS) + 1:
        return

    df = pd.DataFrame(dq)
    dirs = []
    for p in config.RSI_PERIODS:
        val = RSIIndicator(df["close"], window=p).rsi().iloc[-1]
        if val > config.RSI_UPPER:
            dirs.append("DOWN")
        elif val < config.RSI_LOWER:
            dirs.append("UP")
        else:
            return  # хоть один RSI в нейтрали – нет сигнала

    if all(d == dirs[0] for d in dirs):
        txt = "Вниз" if dirs[0] == "DOWN" else "Вверх"
        ts  = df["time"].iloc[-1]
        key = (pair, ts, txt)
        if key not in sent_signals:
            sent_signals.add(key)
            msg = f"{pair} | экспирация {config.EXPIRATION_MIN} мин | {txt}"
            try:
                bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg)
                logger.info("✅ Signal sent: %s", msg)
            except Exception as e:
                logger.error("Telegram error: %s", e)

# 6) Socket.IO handlers

@sio.event
def connect():
    logger.info("🟢 Connected to ws.pocketoption.com (polling)")
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
    inst = msg.get("instrument")
    if inst in config.OTC_PAIRS and msg.get("timeframe") == 60:
        on_new_candle(inst, msg.get("candle", {}))

@sio.event
def disconnect():
    logger.warning("🔴 Disconnected from PO")

def start_polling():
    sio.connect(
        "https://ws.pocketoption.com",
        transports=["polling"],
        headers={"Origin": "https://pocketoption.com"},
        socketio_path="socket.io"
    )
    sio.wait()

if __name__ == "__main__":
    threading.Thread(target=start_polling, daemon=True).start()
    logger.info("🤖 Bot running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down…")
        sio.disconnect()
