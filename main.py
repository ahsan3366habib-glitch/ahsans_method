import os
import logging
import threading
import asyncio
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
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

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

# Authorized testing only:
# This bot requires an explicit confirmation before purchasing.
# It does not expose public/free OTP boards or scraping functionality.


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

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(API_BASE_URL, params=params) as response:
            response.raise_for_status()
            return await response.json()


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

    return data if isinstance(data, list) else []


async def buy_number(country, product, operator="any"):
    return await api_get(
        "buy",
        country=country,
        product=product,
        operator=operator,
    )


async def check_order(order_id):
    return await api_get("check", order_id=order_id)


async def cancel_order(order_id):
    return await api_get("cancel", order_id=order_id)


async def finish_order(order_id):
    return await api_get("finish", order_id=order_id)


def user_order(context, user_id):
    orders = context.user_data.setdefault("orders", {})
    return orders.get(str(user_id))


def save_order(context, user_id, order):
    orders = context.user_data.setdefault("orders", {})
    orders[str(user_id)] = order


def clear_order(context, user_id):
    orders = context.user_data.setdefault("orders", {})
    orders.pop(str(user_id), None)


def mask_phone(phone):
    phone = str(phone or "")
    if len(phone) <= 6:
        return "*" * len(phone)
    return phone[:4] + "*" * max(3, len(phone) - 8) + phone[-4:]


def extract_status(data):
    if not isinstance(data, dict):
        return ""
    return str(
        data.get("status")
        or data.get("state")
        or data.get("response")
        or ""
    ).lower()


def extract_otp(data):
    if not isinstance(data, dict):
        return None

    # Common OTP/SMS field names. We only display a code returned by
    # the reseller API for an order owned by the current bot user.
    for key in ("otp", "code", "sms_code", "verification_code"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)

    sms = data.get("sms")
    if isinstance(sms, dict):
        for key in ("otp", "code", "text"):
            value = sms.get(key)
            if value not in (None, ""):
                return str(value)

    return None


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin access নেই।")
        return

    await update.message.reply_text(
        "🛠️ <b>Admin Panel</b>\n\n"
        "✅ Bot is online\n"
        f"👤 Your Admin ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 API Countries", callback_data="admin_countries")]
        ]),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 <b>Authorized Testing Bot</b>\n\n"
        "নিজের/অনুমোদিত testing-এর জন্য country → service → operator নির্বাচন করো।",
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
        logger.exception("Countries API error")
        await query.edit_message_text("❌ Country list load করা যায়নি।")
        return

    if not countries:
        await query.edit_message_text("❌ কোনো country পাওয়া যায়নি।")
        return

    buttons = []
    for code, info in countries.items():
        if isinstance(info, dict):
            name = info.get("text_en") or info.get("name") or str(code).title()
        else:
            name = str(info or code).title()

        buttons.append([
            InlineKeyboardButton(
                f"🌍 {name}",
                callback_data=f"country:{code}",
            )
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
        await query.edit_message_text("❌ Products load করা যায়নি।")
        return

    if not products:
        await query.edit_message_text("❌ কোনো product পাওয়া যায়নি।")
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

    buttons.append([
        InlineKeyboardButton("⬅️ Countries", callback_data="countries")
    ])

    await query.edit_message_text(
        f"🌍 <b>{country.title()}</b>\n\n"
        "📱 <b>Select Service</b>",
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

    buttons = []

    for index, op in enumerate(operators):
        if not isinstance(op, dict):
            continue

        name = op.get("name", op.get("operator", "any"))
        price = op.get("customer_price", op.get("price", "-"))
        available = op.get("available_count", op.get("Qty", 0))

        # Use index rather than operator name in callback to avoid
        # callback-data length/character issues.
        buttons.append([InlineKeyboardButton(
            f"⚙️ {name} | ${price} | 📦 {available}",
            callback_data=f"op:{country}:{product}:{index}",
        )])

    context_operators = operators

    # Save the exact list for this user/session.
    query.message.chat_id
    # The callback handler will refresh the list when needed.

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Services",
            callback_data=f"country:{country}",
        )
    ])

    await query.edit_message_text(
        f"🌍 <b>{country.title()}</b>\n"
        f"📱 <b>{product.title()}</b>\n\n"
        "⚙️ <b>Select Operator</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_confirm(query, country, product, operator):
    await query.answer()

    # Refresh operator information so the confirmation price is current.
    try:
        operators = await get_operators(country, product)
    except Exception:
        logger.exception("Operator refresh error")
        await query.edit_message_text("❌ Operator তথ্য পাওয়া যায়নি।")
        return

    selected = None
    for op in operators:
        if isinstance(op, dict):
            op_name = str(op.get("name", op.get("operator", "any")))
            if op_name == operator:
                selected = op
                break

    price = "-"
    available = "-"
    if selected:
        price = selected.get("customer_price", selected.get("price", "-"))
        available = selected.get("available_count", selected.get("Qty", "-"))

    # Save pending purchase details.
    query.bot_data.setdefault("pending", {})[query.from_user.id] = {
        "country": country,
        "product": product,
        "operator": operator,
        "price": price,
    }

    await query.edit_message_text(
        "⚠️ <b>Purchase Confirmation</b>\n\n"
        f"🌍 Country: <b>{country}</b>\n"
        f"📱 Service: <b>{product}</b>\n"
        f"⚙️ Operator: <b>{operator}</b>\n"
        f"💰 Price: <b>${price}</b>\n"
        f"📦 Available: <b>{available}</b>\n\n"
        "শুধু নিজের/অনুমোদিত testing-এর জন্য purchase করো।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Purchase", callback_data="confirm_buy")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_pending")],
        ]),
    )


async def purchase_confirmed(query, context):
    await query.answer("Processing...")

    pending = query.bot_data.setdefault("pending", {}).pop(
        query.from_user.id, None
    )

    if not pending:
        await query.edit_message_text("❌ Purchase session পাওয়া যায়নি। আবার শুরু করো।")
        return

    try:
        result = await buy_number(
            pending["country"],
            pending["product"],
            pending["operator"],
        )
    except Exception:
        logger.exception("Buy API error")
        await query.edit_message_text("❌ Purchase request failed।")
        return

    if not isinstance(result, dict) or str(result.get("status", "")).lower() != "success":
        safe_result = result if isinstance(result, dict) else {"response": str(result)}
        message = safe_result.get("message") or safe_result.get("error") or "Purchase failed."
        await query.edit_message_text(f"❌ {message}")
        return

    order_id = result.get("order_id")
    phone = result.get("phone")
    operator = result.get("operator", pending["operator"])
    price = result.get("price", pending["price"])

    if not order_id:
        await query.edit_message_text("❌ API order_id দেয়নি। Purchase result Logs-এ দেখো।")
        return

    order = {
        "order_id": order_id,
        "phone": phone,
        "country": pending["country"],
        "product": pending["product"],
        "operator": operator,
        "price": price,
        "started_at": asyncio.get_running_loop().time(),
        "polling": True,
    }

    save_order(context, query.from_user.id, order)

    await query.edit_message_text(
        "✅ <b>Order Created</b>\n\n"
        f"🆔 Order: <code>{order_id}</code>\n"
        f"📞 Number: <code>{mask_phone(phone)}</code>\n"
        f"⚙️ Operator: <b>{operator}</b>\n"
        f"💰 Price: <b>${price}</b>\n\n"
        "⏳ SMS/OTP-এর জন্য অপেক্ষা করছি। সর্বোচ্চ 120 সেকেন্ড।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")
        ]]),
    )

    context.application.create_task(
        poll_order(context.application, context, query.from_user.id)
    )


async def poll_order(application, context, user_id):
    # Poll every 3 seconds for at most 120 seconds.
    for _ in range(40):
        await asyncio.sleep(3)

        order = user_order(context, user_id)
        if not order or not order.get("polling"):
            return

        try:
            result = await check_order(order["order_id"])
        except Exception:
            logger.exception("Check API error")
            continue

        otp = extract_otp(result)

        if otp:
            order["polling"] = False
            save_order(context, user_id, order)

            try:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "📩 <b>OTP Received</b>\n\n"
                        f"🆔 Order: <code>{order['order_id']}</code>\n"
                        f"📞 Number: <code>{mask_phone(order['phone'])}</code>\n"
                        f"🔐 OTP: <code>{otp}</code>\n\n"
                        "Testing complete হলে Finish চাপতে পারো।"
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "✅ Finish Order",
                            callback_data="finish_order"
                        )]
                    ]),
                )
            except Exception:
                logger.exception("Could not send OTP message")

            return

    # No OTP after 120 seconds: cancel/refund.
    order = user_order(context, user_id)
    if not order or not order.get("polling"):
        return

    try:
        result = await cancel_order(order["order_id"])
        logger.info("Automatic cancel completed for order %s: %s",
                    order["order_id"], result)
    except Exception:
        logger.exception("Automatic cancel API error")

    order["polling"] = False
    clear_order(context, user_id)

    try:
        await application.bot.send_message(
            chat_id=user_id,
            text=(
                "⏱️ <b>120 seconds শেষ</b>\n\n"
                "📭 কোনো SMS/OTP পাওয়া যায়নি।\n"
                "❌ Order cancel/refund request পাঠানো হয়েছে।"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Could not send timeout message")


async def cancel_current(query, context):
    await query.answer("Cancelling...")

    order = user_order(context, query.from_user.id)

    if not order:
        await query.edit_message_text("ℹ️ কোনো active order নেই।")
        return

    try:
        result = await cancel_order(order["order_id"])
    except Exception:
        logger.exception("Cancel API error")
        await query.edit_message_text("❌ Cancel request failed।")
        return

    order["polling"] = False
    clear_order(context, query.from_user.id)

    await query.edit_message_text(
        "❌ <b>Order Cancelled</b>\n\n"
        f"🆔 Order: <code>{order['order_id']}</code>\n"
        "💰 API অনুযায়ী refund process করা হয়েছে।",
        parse_mode="HTML",
    )


async def finish_current(query, context):
    await query.answer("Finishing...")

    order = user_order(context, query.from_user.id)

    if not order:
        await query.edit_message_text("ℹ️ কোনো active order নেই।")
        return

    try:
        result = await finish_order(order["order_id"])
    except Exception:
        logger.exception("Finish API error")
        await query.edit_message_text("❌ Finish request failed।")
        return

    order["polling"] = False
    clear_order(context, query.from_user.id)

    await query.edit_message_text(
        "✅ <b>Order Finished</b>\n\n"
        f"🆔 Order: <code>{order['order_id']}</code>\n"
        "Order permanently saved by the reseller API.",
        parse_mode="HTML",
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "admin_countries":
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("⛔ Admin access নেই.", show_alert=True)
            return
        try:
            countries = await get_countries()
            await query.answer(f"{len(countries)} countries")
            await query.edit_message_text(
                f"📊 <b>Admin Info</b>\n\n🌍 Countries available: <b>{len(countries)}</b>",
                parse_mode="HTML",
            )
        except Exception:
            await query.answer("API error", show_alert=True)
        return

    if data == "countries":
        await show_countries(query)
        return

    if data.startswith("country:"):
        await show_products(query, data.split(":", 1)[1])
        return

    if data.startswith("product:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            await show_operators(query, parts[1], parts[2])
        else:
            await query.answer("Invalid selection.")
        return

    if data.startswith("op:"):
        parts = data.split(":", 3)
        if len(parts) != 4:
            await query.answer("Invalid operator.")
            return

        country, product, index_text = parts[1], parts[2], parts[3]

        try:
            index = int(index_text)
            operators = await get_operators(country, product)
            operator = operators[index].get(
                "name",
                operators[index].get("operator", "any"),
            )
        except Exception:
            await query.answer("Operator data expired. Please retry.")
            return

        await show_confirm(query, country, product, str(operator))
        return

    if data == "confirm_buy":
        await purchase_confirmed(query, context)
        return

    if data == "cancel_pending":
        context.bot_data.setdefault("pending", {}).pop(
            query.from_user.id, None
        )
        await query.answer("Cancelled")
        await query.edit_message_text(
            "❌ Purchase cancelled। /start দিয়ে আবার শুরু করো।"
        )
        return

    if data == "cancel_order":
        await cancel_current(query, context)
        return

    if data == "finish_order":
        await finish_current(query, context)
        return

    await query.answer()


async def error_handler(update, context):
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)


def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    logger.info("Telegram bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
