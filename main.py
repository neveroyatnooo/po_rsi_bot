import time, logging, threading
from collections import deque

import pandas as pd
from ta.momentum import RSIIndicator
from telegram import Bot
import socketio, requests
import config

# отключаем SSL-warnings
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger()

# Telegram-бот
bot = Bot(token=config.TELEGRAM_TOKEN)

# HTTP-сессия (для polling-transport)
session = requests.Session()
session.verify = False

# Socket.IO клиент
sio = socketio.Client(http_session=session)

# Стратегия: храним свечи и набор отправленных сигналов
hist = {p: deque(maxlen=100) for p in config.OTC_PAIRS}
sent = set()

def on_new_candle(pair, candle):
    dq = hist[pair]
    dq.append({"time":candle["from"],"open":candle["open"],
               "high":candle["high"],"low":candle["low"],
               "close":candle["close"]})
    if len(dq) < max(config.RSI_PERIODS)+1: return
    df = pd.DataFrame(dq)
    dirs=[]
    for per in config.RSI_PERIODS:
        v=RSIIndicator(df["close"], window=per).rsi().iloc[-1]
        if v>config.RSI_UPPER:   dirs.append("DOWN")
        elif v<config.RSI_LOWER: dirs.append("UP")
        else: return
    if all(d==dirs[0] for d in dirs):
        txt="Вниз" if dirs[0]=="DOWN" else "Вверх"
        ts=df["time"].iloc[-1]
        key=(pair,ts,txt)
        if key not in sent:
            sent.add(key)
            msg=f"{pair} | экспирация {config.EXPIRATION_MIN} мин | {txt}"
            try:
                bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=msg)
                logger.info("✅ Signal sent: %s", msg)
            except Exception as e:
                logger.error("Telegram error: %s", e)

# Socket.IO handlers
@sio.event
def connect():
    logger.info("🟢 Connected to %s (polling)", config.PO_SOCKET_HOST)
    sio.emit("authenticate", {"email":config.PO_EMAIL, "password":config.PO_PASSWORD})

@sio.on("auth_response")
def on_auth(data):
    if data.get("status")=="ok":
        logger.info("🔓 Auth OK")
        for p in config.OTC_PAIRS:
            sio.emit("subscribe_candles", {"instrument":p, "timeframe":60})
    else:
        logger.error("❌ Auth failed: %s", data)

@sio.on("candle")
def on_candle(msg):
    if msg.get("instrument") in config.OTC_PAIRS and msg.get("timeframe")==60:
        on_new_candle(msg["instrument"], msg["candle"])

@sio.event
def disconnect(*args):
    logger.warning("🔴 Disconnected, args=%s", args)

def start_polling():
    url=f"https://{config.PO_SOCKET_HOST}"
    while True:
        try:
            logger.info("🔄 Connecting to %s …", url)
            sio.connect(
                url,
                transports=["polling"],
                headers={"Origin":"https://pocketoption.com"},
                socketio_path=config.PO_SOCKET_PATH,
                wait_timeout=20
            )
            sio.wait()
        except Exception as e:
            logger.error("❌ Connection error: %s", e)
        finally:
            try: sio.disconnect()
            except: pass
        logger.info("⏳ Reconnect in 5s…")
        time.sleep(5)

if __name__=="__main__":
    threading.Thread(target=start_polling, daemon=True).start()
    logger.info("🤖 Bot is running. Ctrl+C to stop.")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down…")
        sio.disconnect()
