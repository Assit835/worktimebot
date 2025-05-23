
import logging
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt
import os
import io
from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
import asyncio

OFFICE_LAT = 57.133063
OFFICE_LON = 65.506559
MAX_DISTANCE_METERS = 100
ADMIN_CHAT_ID = 1187398378
ASK_NAME = 1
report_tables = {}

conn = sqlite3.connect("attendance.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS employees (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    expected_start_time TEXT DEFAULT '10:00')''')
cursor.execute('''CREATE TABLE IF NOT EXISTS actions (
    user_id INTEGER PRIMARY KEY,
    action TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS attendance (
    user_id INTEGER, username TEXT, date TEXT, time_in TEXT, time_out TEXT,
    lat_in REAL, lon_in REAL, lat_out REAL, lon_out REAL)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS tardiness (
    user_id INTEGER, date TEXT, time_in TEXT, delay_minutes INTEGER)''')
conn.commit()

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
application = None

def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 2 * asin(sqrt(a)) * 6371 * 1000

def is_registered(user_id):
    cursor.execute("SELECT name FROM employees WHERE user_id=?", (user_id,))
    return cursor.fetchone()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if is_registered(user_id):
        return await show_main_menu(update)
    await update.message.reply_text("Привет! Как тебя зовут?")
    return ASK_NAME

async def save_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    user_id = update.message.from_user.id
    cursor.execute(
        "REPLACE INTO employees (user_id, name, expected_start_time) VALUES (?, ?, ?)",
        (user_id, name, "10:00")
    )
    conn.commit()
    await update.message.reply_text(f"Спасибо, {name}!")
    return await show_main_menu(update)

async def show_main_menu(update: Update):
    keyboard = [[KeyboardButton("Пришел")], [KeyboardButton("Ушел")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    return ConversationHandler.END

async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = update.message.text.lower()
    if action not in ["пришел", "ушел"]:
        await update.message.reply_text("Пожалуйста, нажмите одну из кнопок.")
        return
    cursor.execute("REPLACE INTO actions (user_id, action) VALUES (?, ?)", (update.message.from_user.id, action))
    conn.commit()
    keyboard = [[KeyboardButton("Отправить локацию", request_location=True)]]
    await update.message.reply_text("Отправь свою геопозицию.", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    location = update.message.location
    lat, lon = location.latitude, location.longitude
    username = user.username or user.first_name

    dist = haversine(OFFICE_LAT, OFFICE_LON, lat, lon)
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    cursor.execute("SELECT action FROM actions WHERE user_id=?", (user_id,))
    res = cursor.fetchone()
    if not res:
        await update.message.reply_text("Сначала выбери действие.")
        return

    action = res[0]
    cursor.execute("DELETE FROM actions WHERE user_id=?", (user_id,))
    conn.commit()

    if action == "пришел":
        if dist <= MAX_DISTANCE_METERS:
            cursor.execute("INSERT INTO attendance (user_id, username, date, time_in, lat_in, lon_in) VALUES (?, ?, ?, ?, ?, ?)",
                           (user_id, username, date_str, time_str, lat, lon))
            conn.commit()

            cursor.execute("SELECT expected_start_time FROM employees WHERE user_id=?", (user_id,))
            expected_str = cursor.fetchone()[0]
            expected_time = datetime.strptime(expected_str, "%H:%M").time()
            expected_dt = datetime.combine(now.date(), expected_time)
            actual_dt = now

            delay_minutes = int((actual_dt - expected_dt).total_seconds() / 60)
            if delay_minutes > 5:
                cursor.execute(
                    "INSERT INTO tardiness (user_id, date, time_in, delay_minutes) VALUES (?, ?, ?, ?)",
                    (user_id, date_str, time_str, delay_minutes)
                )
                conn.commit()
                await update.message.reply_text(f"⚠️ Опоздание на {delay_minutes} минут.")
            else:
                await update.message.reply_text(f"✅ Приход отмечен. Расстояние: {int(dist)} м. Без опоздания.")
        else:
            await update.message.reply_text(f"❌ Вне офиса. Расстояние: {int(dist)} м.")
    elif action == "ушел":
        cursor.execute("SELECT * FROM attendance WHERE user_id=? AND date=? AND time_out IS NULL", (user_id, date_str))
        if cursor.fetchone():
            cursor.execute("UPDATE attendance SET time_out=?, lat_out=?, lon_out=? WHERE user_id=? AND date=?",
                           (time_str, lat, lon, user_id, date_str))
            conn.commit()
            await update.message.reply_text(f"✅ Уход отмечен. Расстояние: {int(dist)} м.")
        else:
            await update.message.reply_text("❗ Сначала отметь приход.")

    await show_main_menu(update)

@app.post("/webhook")
async def webhook():
    data = request.get_json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return "ok", 200

@app.get("/")
def root():
    return "Бот работает!"

async def main():
    global application
    token = os.getenv("TELEGRAM_TOKEN")
    application = ApplicationBuilder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)]},
        fallbacks=[],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_action))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))

    await application.initialize()
    await application.start()
    webhook_url = os.getenv("WEBHOOK_URL")
    await application.bot.set_webhook(webhook_url)
    print("Webhook установлен:", webhook_url)

    import threading
    threading.Thread(target=app.run, kwargs={"host": "0.0.0.0", "port": int(os.environ.get("PORT", 5000))}).start()

asyncio.run(main())
