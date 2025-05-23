import logging
import os
import io
import sqlite3
import pandas as pd
from flask import Flask, request, Response
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

# --- Flask ---
flask_app = Flask(__name__)

# --- Telegram init ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://your-render-url/webhook
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

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
            expected = cursor.fetchone()[0]
            expected_dt = datetime.strptime(expected, "%H:%M")
            actual_dt = datetime.strptime(time_str, "%H:%M:%S")
            delay = int((actual_dt - expected_dt).total_seconds() / 60)
            if delay > 5:
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

# --- Flask Webhook Route ---
@flask_app.route("/webhook", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return Response("ok", status=200)

# --- Регистрация хендлеров ---
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

# --- Запуск ---
if __name__ == "__main__":
    import asyncio
    asyncio.run(application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook"))
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
