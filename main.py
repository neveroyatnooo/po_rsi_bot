# main.py

# ── 1) Monkey-patch, чтобы отключить внутренние EIO/SocketIO-disconnect-handlers
import engineio.base_client
engineio.base_client.BaseClient._handle_eio_disconnect = lambda *a, **k: None

import socketio.client
socketio.client.Client._handle_eio_disconnect = lambda *a, **k: None

# ── 2) Обычные импорты
import time
import logging
import threading
from collections import deque

import pandas as pd
from ta.momentum import RSIIndicator
from telegram import Bot
import socketio
import requests
import urllib3

import config

# ── 3) Отключаем InsecureRequestWarning (verify=False)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 4) Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger()

# ── 5) Telegram-бот
bot = Bot(token=config.TELEGRAM_TOKEN)

# ── 6) HTTP-сессия (для Engine.IO polling-транспорта)
session = requests.Session()
session.verify = False

# ── 7) Инициализируем Socket.IO-клиент
sio = socketio.Client(http_session=session)

# ── 8) Структуры для истории свечей и уже отправленных сигналов
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
    # ждём минимум max_period+1
    if len(dq) < max(config.RSI_PERIODS) + 1:
        return

    df = pd.DataFrame(dq)
    dirs = []
    for p in config.RSI_PERIODS:
        r = RSIIndicator(df["close"], window=p).rsi().iloc[-1]
        if r > config.RSI_UPPER:
            dirs.append("DOWN")
        elif r < config.RSI_LOWER:
            dirs.append("UP")
        else:
            return

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

# ── 9) Socket.IO handlers ───────────────────────────────────────────────
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
    if msg.get("instrument") in config.OTC_PAIRS and msg.get("timeframe") == 60:
        on_new_candle(msg["instrument"], msg["candle"])

@sio.event
def disconnect(*args):
    logger.warning("🔴 Disconnected, args=%s", args)

# ── 10) Loop-реконнект с явным EIO=3-handshake ──────────────────────────
def start_polling():
    base = f"https://{config.PO_SOCKET_HOST}"
    # в руку вшит EIO=3 и polling
    handshake = f"/{config.PO_SOCKET_PATH}/?EIO=3&transport=polling"
    url = base + handshake

    while True:
        try:
            logger.info("🔄 Connecting to %s …", url)
            sio.connect(
                url,
                transports=["polling"],
                headers={"Origin": "https://pocketoption.com"},
                socketio_path="",      # путь уже в URL
                wait_timeout=20
            )
            sio.wait()
        except Exception as e:
            logger.error("❌ Connection error: %s", e)
        finally:
            try:
                sio.disconnect()
            except:
                pass
        logger.info("⏳ Reconnect in 5s…")
        time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=start_polling, daemon=True).start()
    logger.info("🤖 Bot is running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down…")
        sio.disconnect()
