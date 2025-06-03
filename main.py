import logging
import os
import io
import sqlite3
import pandas as pd
import re
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt
from datetime import timezone
import pytz
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

# --- Константы и переменные ---
OFFICE_LAT = 57.133063
OFFICE_LON = 65.506559
MAX_DISTANCE_METERS = 100
ADMIN_CHAT_ID = 1187398378
ASK_NAME = 1
report_tables = {}

# --- База данных ---
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
    local_tz = pytz.timezone("Asia/Yekaterinburg")  # замени на нужную тебе зону
    now = datetime.now(local_tz)

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
            res = cursor.fetchone()
            if res is None or res[0] is None:
                expected = "10:00"
            else:
                expected = res[0]


            if not re.match(r"^\d{1,2}:\d{2}$", expected):
                expected = "10:00"

            today = datetime.now().date()
            expected_dt = local_tz.localize(datetime.combine(today, datetime.strptime(expected, "%H:%M").time()))
            actual_dt = now  # он уже с нужной зоной
            delay = (actual_dt - expected_dt).total_seconds() / 60


            logging.info(f"User {user_id} expected: {expected_dt.time()}, actual: {actual_dt.time()}, delay: {delay} min")

            if delay > 0:
                delay = int(delay)
                cursor.execute("INSERT INTO tardiness (user_id, date, time_in, delay_minutes) VALUES (?, ?, ?, ?)",
                               (user_id, date_str, time_str, delay))
                conn.commit()
                await update.message.reply_text(f"⚠️ Опоздание на {delay} минут.")

            await update.message.reply_text(f"✅ Приход отмечен. Расстояние: {int(dist)} м.")
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

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return
    keyboard = [
        [InlineKeyboardButton("Сегодня", callback_data="report_today")],
        [InlineKeyboardButton("7 дней", callback_data="report_7")],
        [InlineKeyboardButton("30 дней", callback_data="report_30")],
        [InlineKeyboardButton("365 дней", callback_data="report_365")]
    ]
    await update.message.reply_text("📊 Выбери период отчета:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_report_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    label_map = {
        "report_today": ("сегодня", 0),
        "report_7": ("7 дней", 7),
        "report_30": ("30 дней", 30),
        "report_365": ("365 дней", 365)
    }

    if query.data not in label_map:
        await query.edit_message_text("Неизвестный период.")
        return

    label, days = label_map[query.data]
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days else datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT user_id, name FROM employees")
    employees = cursor.fetchall()

    report_lines = [f"📊 Отчет за {label}:"]
    table = []

    for user_id, name in employees:
        cursor.execute("SELECT COUNT(*), AVG(delay_minutes) FROM tardiness WHERE user_id=? AND date >= ?", (user_id, start_date))
        count, avg = cursor.fetchone()
        if count:
            avg_delay = int(avg)
            report_lines.append(f"— {name}: {count} опозданий (ср. {avg_delay} мин)")
            table.append({"Сотрудник": name, "Кол-во опозданий": count, "Средняя задержка (мин)": avg_delay})
        else:
            report_lines.append(f"— {name}: без опозданий")
            table.append({"Сотрудник": name, "Кол-во опозданий": 0, "Средняя задержка (мин)": "—"})

    report_tables[query.from_user.id] = pd.DataFrame(table)

    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📎 Скачать таблицу", callback_data=f"download_excel_{label}")]
    ])
    await query.edit_message_text("\n".join(report_lines), reply_markup=reply_markup)

async def handle_excel_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in report_tables:
        await query.edit_message_text("❌ Нет доступного отчета. Сначала запроси /report.")
        return

    df = report_tables[user_id]
    excel_buffer = io.BytesIO()
    df.to_excel(excel_buffer, index=False)
    excel_buffer.seek(0)

    await context.bot.send_document(
        chat_id=user_id,
        document=excel_buffer,
        filename="report.xlsx"
    )

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Неизвестная команда. Используй кнопки или /report.")

# --- Инициализация приложения ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Например: https://your-app.onrender.com

application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

application.add_handler(ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)]},
    fallbacks=[], allow_reentry=True
))
application.add_handler(CommandHandler("report", report))
application.add_handler(CallbackQueryHandler(handle_report_button, pattern="^report_"))
application.add_handler(CallbackQueryHandler(handle_excel_download, pattern="^download_excel_"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_action))
application.add_handler(MessageHandler(filters.LOCATION, handle_location))
application.add_handler(MessageHandler(filters.COMMAND, unknown))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

def run_health_server():
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()


if __name__ == "__main__":
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        url_path="webhook",
        webhook_url=f"{WEBHOOK_URL}/webhook"
    )
