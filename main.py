import logging
from datetime import datetime
from functools import partial
import asyncio

import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    JobQueue,
)


# ───────────── Конфигурация ──────────────
TELEGRAM_TOKEN  = '7777081301:AAHLuvi5X5LtFmRZsi4mGxTbMLsv6rQ9ex4'
CHAT_ID         = 981037533
MT5_LOGIN       = 10006437099
MT5_PASSWORD    = '1lBsS-Fg'
MT5_SERVER      = 'MetaQuotes-Demo'

CURRENCY_PAIRS  = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY", "CADJPY", "CHFJPY", "EURAUD", "EURCHF", "CADCHF", "EURGBP", "EURCHF", "AUDCHF"]
RSI_PERIODS     = [7, 14, 21]
RSI_OVERBOUGHT  = 70
RSI_OVERSOLD    = 30
VOL_THRESHOLDS  = {'low':0.0005, 'high':0.001}
CONF_THRESHOLD  = 0.70
POLL_INTERVAL   = 3.0 

open_signals       = set()

# вместо VOL_THRESHOLDS и функции choose_expiration
EXPIRATION_MINUTES = 3


SEND_SEMAPHORE = asyncio.Semaphore(5)

statistics = {'wins': 0, 'losses': 0}
# ──────────────────────────────────────────

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)


async def init_mt5() -> bool:
    if not mt5.initialize():
        logging.error("MT5 init error: %s", mt5.last_error())
        return False
    if not mt5.login(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        logging.error("MT5 login error: %s", mt5.last_error())
        return False
    logging.info("MT5 initialized and logged in")
    return True

def fetch_1m(symbol: str, bars: int = 100) -> pd.DataFrame:
    # Пропускаем текущий незакрытый бар
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, bars)
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.sort_values('time', inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df

def get_last_rsi_values(df: pd.DataFrame) -> list[float]:
    vals = []
    for p in RSI_PERIODS:
        df[f"rsi_{p}"] = ta.rsi(df['close'], length=p)
        vals.append(df[f"rsi_{p}"].iat[-1])
    return vals

def calc_confidence(rsi_vals: list[float]) -> float:
    return sum(abs(v - 50) / 50 for v in rsi_vals) / len(rsi_vals)

def inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Показать статистику", callback_data="stats")],
        [InlineKeyboardButton("🔄 Сбросить статистику", callback_data="reset")],
    ])

async def safe_send(bot, **kwargs):
    async with SEND_SEMAPHORE:
        return await bot.send_message(**kwargs)

async def process_trade(symbol: str, bot):
    """
    Обрабатываем одну пару:
    - считаем RSI
    - если сигнал и он не открыт, отправляем
    - имитируем сделку и отправляем результат
    """
    loop = asyncio.get_running_loop()
    key = None

    try:
        # 1) Получаем закрытые бары
        df = await loop.run_in_executor(None, partial(fetch_1m, symbol, 200))

        # 2) Считаем RSI и логируем
        rsi_vals = get_last_rsi_values(df)
        logging.info(f"[DEBUG] {symbol} RSI → {rsi_vals}")

        conf = calc_confidence(rsi_vals)
        logging.info(f"[DEBUG] {symbol} RSI → {rsi_vals}; вероятность={conf:.2f}")

        # 3) Определяем направление сигнала
        if all(v < RSI_OVERSOLD   for v in rsi_vals):
            direction = 'UP'
        elif all(v > RSI_OVERBOUGHT for v in rsi_vals):
            direction = 'DOWN'
        else:
            return

        # 4) Проверка уверенности
        conf = calc_confidence(rsi_vals)
        if conf < CONF_THRESHOLD:
            logging.info(f"[{symbol}] conf={conf:.2f} < {CONF_THRESHOLD}, skip")
            return

        # 5) Формируем ключ и проверяем, не открыт ли уже такой сигнал
        key = (symbol, direction)
        if key in open_signals:
            logging.info(f"[{symbol}] signal {direction} already open, skip")
            return

        # 6) Отправляем сигнал
        expm = EXPIRATION_MINUTES
        sig_msg = await safe_send(
            bot,
            chat_id=CHAT_ID,
            text=(
                f"📊 <b>{symbol}</b>\n"
                f"⏱ Экспирация: <b>{expm} мин</b>\n"
                f"🔀 Направление: <b>{direction}</b>\n"
                f"🧠 Уверенность: <b>{conf:.2f}</b>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=inline_keyboard()
        )
        logging.info(f"[{symbol}] SIGNAL {direction} exp={expm}min conf={conf:.2f}")

        # отмечаем сигнал как «открытый»
        open_signals.add(key)

        # 7) Имитация входа через 10 секунд
        await asyncio.sleep(10)
        tick = await loop.run_in_executor(None, partial(mt5.symbol_info_tick, symbol))
        entry_price = tick.ask if direction == 'UP' else tick.bid
        logging.info(f"[{symbol}] Entry price: {entry_price:.5f}")

        # 8) Ждём EXPIRATION
        await asyncio.sleep(expm * 60)
        tick2 = await loop.run_in_executor(None, partial(mt5.symbol_info_tick, symbol))
        exit_price = tick2.bid if direction == 'UP' else tick2.ask
        logging.info(f"[{symbol}] Exit price:  {exit_price:.5f}")

        # 9) Результируем
        success = (exit_price > entry_price and direction == 'UP') or \
                  (exit_price < entry_price and direction == 'DOWN')
        diff = abs(exit_price - entry_price)
        if success:
            statistics['wins'] += 1
        else:
            statistics['losses'] += 1

        await safe_send(
            bot,
            chat_id=CHAT_ID,
            text=(f"{'✅ Успешно' if success else '❌ Неуспешно'}\n"
                  f"Δ = <b>{diff:.5f}</b>"),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=sig_msg.message_id
        )
        logging.info(f"[{symbol}] RESULT {'OK' if success else 'FAIL'} Δ={diff:.5f}")

    except Exception as e:
        logging.error(f"[{symbol}] Exception: {e}")

    finally:
        # снимаем блокировку сигнала
        if key in open_signals:
            open_signals.remove(key)

async def scan_signals(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    for sym in CURRENCY_PAIRS:
        context.application.create_task(process_trade(sym, bot))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wins, losses = statistics['wins'], statistics['losses']
    total = wins + losses
    txt = (
        f"Всего сигналов: {total}\n"
        f"✅ Успешных: {wins}\n"
        f"❌ Неуспешных: {losses}"
    )
    await update.effective_message.reply_text(txt, reply_markup=inline_keyboard())

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    statistics['wins'] = statistics['losses'] = 0
    await stats_command(update, context)

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == 'stats':
        await stats_command(update, context)
    else:
        await reset_command(update, context)

async def on_startup(app):
    if not await init_mt5():
        logging.error("MT5 init failed — stopping bot")
        await app.stop()

def main():
    app = ApplicationBuilder()\
        .token(TELEGRAM_TOKEN)\
        .post_init(on_startup)\
        .build()

    app.add_handler(CommandHandler("stats",  stats_command))
    app.add_handler(CommandHandler("reset",  reset_command))
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # сканируем сигналы каждую POLL_INTERVAL секунду
    app.job_queue.run_repeating(scan_signals,
                                interval=POLL_INTERVAL,
                                first=POLL_INTERVAL)

    logging.info("Bot started, polling…")
    app.run_polling(close_loop=False)

if __name__ == '__main__':
    main()
