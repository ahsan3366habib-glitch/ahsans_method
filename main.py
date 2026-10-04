import os
import logging
import threading
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL")
API_KEY = os.getenv("API_KEY")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not API_BASE_URL:
    raise RuntimeError("API_BASE_URL is missing")
if not API_KEY:
    raise RuntimeError("API_KEY is missing")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Telegram bot is running.")

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("Health server listening on port %s", PORT)
    server.serve_forever()


def sanitize_for_log(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, val in value.items():
            key_lower = str(key).lower()
            if any(word in key_lower for word in ("key", "token", "secret", "password", "authorization")):
                cleaned[key] = "***REDACTED***"
            else:
                cleaned[key] = sanitize_for_log(val)
        return cleaned
    if isinstance(value, list):
        return [sanitize_for_log(x) for x in value[:20]]
    if isinstance(value, str):
        return value[:120] + "...[TRUNCATED]" if len(value) > 120 else value
    return value


async def api_get(action, **params):
    params["key"] = API_KEY
    params["action"] = action

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(API_BASE_URL, params=params) as response:
            response.raise_for_status()
            data = await response.json()

            safe = sanitize_for_log(data)
            logger.info(
                "API RESPONSE action=%s structure=%s",
                action,
                json.dumps(safe, ensure_ascii=False)[:5000],
            )
            return data


async def get_countries():
    data = await api_get("countries")
    if isinstance(data, dict):
        if isinstance(data.get("countries"), dict):
            return data["countries"]
        if isinstance(data.get("data"), dict):
            return data["data"]
        if "status" not in data and "message" not in data:
            return data
    return {}


async def get_products(country):
    data = await api_get("products", country=country)
    if isinstance(data, dict):
        if isinstance(data.get("products"), dict):
            return data["products"]
        if isinstance(data.get("data"), dict):
            return data["data"]
    return {}


async def get_operators(country, product):
    data = await api_get("operators", country=country, product=product)
    if isinstance(data, dict):
        if isinstance(data.get("operators"), list):
            return data["operators"]
        if isinstance(data.get("data"), list):
            return data["data"]
    if isinstance(data, list):
        return data
    return []


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Welcome!</b>\n\nCountry, service এবং operator-এর তথ্য দেখতে নিচের button চাপো।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🌍 Countries", callback_data="countries")
        ]]),
    )


async def show_countries(query):
    await query.answer()
    try:
        countries = await get_countries()
    except Exception:
        logger.exception("Country API error")
        await query.edit_message_text("❌ API call failed. Render Logs-এ API RESPONSE দেখো।")
        return

    if not countries:
        await query.edit_message_text("❌ কোনো country পাওয়া যায়নি। Render Logs-এ API RESPONSE action=countries দেখো।")
        return

    buttons = []
    for code, info in countries.items():
        name = info.get("text_en") or info.get("name") or str(code).title() if isinstance(info, dict) else str(info or code).title()
        buttons.append([InlineKeyboardButton(f"🌍 {name}", callback_data=f"country:{code}")])

    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="countries")])
    await query.edit_message_text(
        "🌍 <b>Select Country</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_products(query, country):
    await query.answer()
    try:
        products = await get_products(country)
    except Exception:
        logger.exception("Products API error")
        await query.edit_message_text("❌ Services load করা যায়নি।")
        return

    if not products:
        await query.edit_message_text("❌ কোনো service পাওয়া যায়নি।")
        return

    buttons = []
    for code, info in products.items():
        info = info if isinstance(info, dict) else {}
        qty = info.get("Qty", info.get("qty", 0))
        price = info.get("Price", info.get("price", "-"))
        buttons.append([InlineKeyboardButton(
            f"📱 {code.title()} | 📦 {qty} | 💰 ${price}",
            callback_data=f"product:{country}:{code}",
        )])

    buttons.append([InlineKeyboardButton("⬅️ Countries", callback_data="countries")])
    await query.edit_message_text(
        f"🌍 <b>{country.title()}</b>\n\n📱 <b>Available Services</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_operators(query, country, product):
    await query.answer()
    try:
        operators = await get_operators(country, product)
    except Exception:
        logger.exception("Operators API error")
        await query.edit_message_text("❌ Operators load করা যায়নি।")
        return

    if not operators:
        await query.edit_message_text("❌ কোনো operator পাওয়া যায়নি।")
        return

    message = f"🌍 <b>{country.title()}</b>\n📱 <b>{product.title()}</b>\n\n⚙️ <b>Available Operators</b>\n\n"
    for op in operators:
        if not isinstance(op, dict):
            continue
        name = op.get("name", op.get("operator", "Unknown"))
        price = op.get("customer_price", op.get("price", "-"))
        available = op.get("available_count", op.get("Qty", 0))
        message += f"🔹 <b>{name}</b>\n💰 Customer Price: ${price}\n📦 Available: {available}\n\n"

    await query.edit_message_text(
        message,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Services", callback_data=f"country:{country}")
        ]]),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "countries":
        await show_countries(query)
    elif data.startswith("country:"):
        await show_products(query, data.split(":", 1)[1])
    elif data.startswith("product:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Invalid selection.")
        else:
            await show_operators(query, parts[1], parts[2])
    else:
        await query.answer()


async def error_handler(update, context):
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)


def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)
    logger.info("Telegram bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
