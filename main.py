
import os
import io
import sqlite3
import logging
import pandas as pd
import re
import threading
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

# --- Константы ---
OFFICE_LAT = 57.133063
OFFICE_LON = 65.506559
MAX_DISTANCE_METERS = 100
ADMIN_CHAT_ID = 1187398378
ASK_NAME = 1
report_tables = {}

# --- База данных ---
conn = sqlite3.connect("attendance.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS employees (user_id INTEGER PRIMARY KEY, name TEXT, expected_start_time TEXT DEFAULT '10:00')")
cursor.execute("CREATE TABLE IF NOT EXISTS actions (user_id INTEGER PRIMARY KEY, action TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS attendance (user_id INTEGER, username TEXT, date TEXT, time_in TEXT, time_out TEXT, lat_in REAL, lon_in REAL, lat_out REAL, lon_out REAL)")
cursor.execute("CREATE TABLE IF NOT EXISTS tardiness (user_id INTEGER, date TEXT, time_in TEXT, delay_minutes INTEGER)")
conn.commit()

# --- Логгирование ---
logging.basicConfig(level=logging.INFO)

# --- Утилиты ---
def haversine(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 2 * asin(sqrt(a)) * 6371 * 1000

def is_registered(user_id):
    cursor.execute("SELECT name FROM employees WHERE user_id=?", (user_id,))
    return cursor.fetchone()

# --- Хендлеры ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if is_registered(user_id):
        return await show_main_menu(update)
    await update.message.reply_text("Привет! Как тебя зовут?")
    return ASK_NAME

async def save_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    user_id = update.message.from_user.id
    cursor.execute("REPLACE INTO employees (user_id, name) VALUES (?, ?)", (user_id, name))
    conn.commit()
    await update.message.reply_text(f"Спасибо, {name}!")
    return await show_main_menu(update)

async def show_main_menu(update: Update):
    keyboard = [[KeyboardButton("Пришел")], [KeyboardButton("Ушел")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Выберите действие:", reply_markup=reply_markup)
    return ConversationHandler.END

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Неизвестная команда. Используй кнопки или /report.")

# --- Healthcheck сервер для Render и UptimeRobot ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

def run_healthcheck_server():
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    server.serve_forever()

# --- Telegram запуск ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

application.add_handler(ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)]},
    fallbacks=[], allow_reentry=True
))
application.add_handler(MessageHandler(filters.COMMAND, unknown))

# Запуск Telegram и / сервера
if __name__ == "__main__":
    threading.Thread(target=run_healthcheck_server, daemon=True).start()
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        url_path="webhook",
        webhook_url=f"{WEBHOOK_URL}/webhook"
    )
