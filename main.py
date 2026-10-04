import os
import logging
import asyncio
import threading
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


async def api_get(action, **params):
    params["key"] = API_KEY
    params["action"] = action

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(API_BASE_URL, params=params) as response:
            response.raise_for_status()
            return await response.json()


async def get_countries():
    data = await api_get("countries")
    return data.get("countries", {}) if data.get("status") == "success" else {}


async def get_products(country):
    data = await api_get("products", country=country)
    return data.get("products", {}) if data.get("status") == "success" else {}


async def get_operators(country, product):
    data = await api_get("operators", country=country, product=product)
    return data.get("operators", []) if data.get("status") == "success" else []


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🌍 Countries", callback_data="countries")]]

    await update.message.reply_text(
        "👋 <b>Welcome!</b>\n\n"
        "Country, service এবং operator-এর বর্তমান তথ্য দেখতে নিচের button চাপো।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_countries(query):
    await query.answer()

    try:
        countries = await get_countries()
    except Exception:
        logger.exception("Country API error")
        await query.edit_message_text("❌ Country list load করা যায়নি।")
        return

    if not countries:
        await query.edit_message_text("❌ কোনো country পাওয়া যায়নি।")
        return

    buttons = []

    for code, info in countries.items():
        name = info.get("text_en", code.title()) if isinstance(info, dict) else code.title()
        buttons.append([
            InlineKeyboardButton(
                f"🌍 {name}",
                callback_data=f"country:{code}",
            )
        ])

    buttons.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="countries")
    ])

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
        await query.edit_message_text("❌ এই country-তে কোনো service নেই।")
        return

    buttons = []

    for code, info in products.items():
        info = info if isinstance(info, dict) else {}

        qty = info.get("Qty", info.get("qty", 0))
        price = info.get("Price", info.get("price", "-"))

        buttons.append([
            InlineKeyboardButton(
                f"📱 {code.title()} | 📦 {qty} | 💰 ${price}",
                callback_data=f"product:{country}:{code}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Countries",
            callback_data="countries",
        )
    ])

    await query.edit_message_text(
        f"🌍 <b>{country.title()}</b>\n\n"
        "📱 <b>Available Services</b>\n\n"
        "📦 = Stock    💰 = Price",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_operators(query, country, product):
    await query.answer()

    try:
        operators = await get_operators(country, product)
    except Exception:
        logger.exception("Operators API error")
        await query.edit_message_text("❌ Operator list load করা যায়নি।")
        return

    if not operators:
        await query.edit_message_text("❌ কোনো active operator পাওয়া যায়নি।")
        return

    message = (
        f"🌍 <b>{country.title()}</b>\n"
        f"📱 <b>{product.title()}</b>\n\n"
        "⚙️ <b>Available Operators</b>\n\n"
    )

    for op in operators:
        if not isinstance(op, dict):
            continue

        name = op.get("name", op.get("operator", "Unknown"))
        price = op.get("customer_price", op.get("price", "-"))
        available = op.get("available_count", op.get("Qty", 0))

        message += (
            f"🔹 <b>{name}</b>\n"
            f"💰 Customer Price: ${price}\n"
            f"📦 Available: {available}\n\n"
        )

    buttons = [[
        InlineKeyboardButton(
            "⬅️ Services",
            callback_data=f"country:{country}",
        )
    ]]

    await query.edit_message_text(
        message,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "countries":
        await show_countries(query)
        return

    if data.startswith("country:"):
        country = data.split(":", 1)[1]
        await show_products(query, country)
        return

    if data.startswith("product:"):
        parts = data.split(":", 2)

        if len(parts) != 3:
            await query.answer("Invalid selection.")
            return

        await show_operators(query, parts[1], parts[2])
        return

    await query.answer()


async def error_handler(update, context):
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)


def main():
    # Render Web Service requires an HTTP listener.
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    logger.info("Telegram bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
