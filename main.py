import os
import json
import sqlite3
import logging
import threading
import asyncio
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY")
PORT = int(os.getenv("PORT", "10000"))
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not API_BASE_URL:
    raise RuntimeError("API_BASE_URL is missing")
if not API_KEY:
    raise RuntimeError("API_KEY is missing")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is missing")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# -----------------------------
# Health server for Render
# -----------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Telegram bot is running.")

    def log_message(self, *_):
        return


def start_health_server():
    HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


# -----------------------------
# Local database
# -----------------------------
db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            banned INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER NOT NULL,
            order_id TEXT,
            country TEXT,
            product TEXT,
            operator TEXT,
            phone TEXT,
            price TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        conn.commit()
        conn.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def upsert_user(user):
    with db_lock:
        conn = db()
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user.id,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE users
                   SET username=?, first_name=?, last_seen=?
                   WHERE user_id=?""",
                (user.username, user.first_name, now(), user.id),
            )
        else:
            conn.execute(
                """INSERT INTO users
                   (user_id, username, first_name, joined_at, last_seen)
                   VALUES (?, ?, ?, ?, ?)""",
                (user.id, user.username, user.first_name, now(), now()),
            )
        conn.commit()
        conn.close()


def is_banned(user_id):
    with db_lock:
        conn = db()
        row = conn.execute(
            "SELECT banned FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        conn.close()
    return bool(row["banned"]) if row else False


def log_admin(admin_id, action):
    with db_lock:
        conn = db()
        conn.execute(
            "INSERT INTO admin_logs(admin_id, action, created_at) VALUES (?, ?, ?)",
            (admin_id, action, now()),
        )
        conn.commit()
        conn.close()


def admin_only(user_id):
    return user_id in ADMIN_IDS


# -----------------------------
# Reseller API
# -----------------------------
async def api_get(action, **params):
    params["key"] = API_KEY
    params["action"] = action

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(API_BASE_URL, params=params) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


def countries_from(data):
    if isinstance(data, dict):
        if isinstance(data.get("countries"), dict):
            return data["countries"]
        if isinstance(data.get("data"), dict):
            return data["data"]
        if "status" not in data and "message" not in data:
            return data
    return {}


def products_from(data):
    if isinstance(data, dict):
        if isinstance(data.get("products"), dict):
            return data["products"]
        if isinstance(data.get("data"), dict):
            return data["data"]
    return {}


def operators_from(data):
    if isinstance(data, dict):
        if isinstance(data.get("operators"), list):
            return data["operators"]
        if isinstance(data.get("data"), list):
            return data["data"]
    return data if isinstance(data, list) else []


async def get_countries():
    return countries_from(await api_get("countries"))


async def get_products(country):
    return products_from(await api_get("products", country=country))


async def get_operators(country, product):
    return operators_from(
        await api_get("operators", country=country, product=product)
    )


async def buy_number(country, product, operator):
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


# -----------------------------
# Helpers
# -----------------------------
def mask_phone(phone):
    s = str(phone or "")
    if len(s) <= 6:
        return "*" * len(s)
    return s[:4] + "*" * max(3, len(s) - 8) + s[-4:]


def otp_from(data):
    if not isinstance(data, dict):
        return None

    for key in ("otp", "code", "sms_code", "verification_code"):
        if data.get(key) not in (None, ""):
            return str(data[key])

    sms = data.get("sms")
    if isinstance(sms, dict):
        for key in ("otp", "code"):
            if sms.get(key) not in (None, ""):
                return str(sms[key])

    return None


def save_order(tg_user_id, result, meta):
    order_id = str(result.get("order_id"))
    with db_lock:
        conn = db()
        conn.execute(
            """INSERT INTO orders
               (telegram_user_id, order_id, country, product, operator,
                phone, price, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tg_user_id, order_id, meta["country"], meta["product"],
                meta["operator"], str(result.get("phone", "")),
                str(result.get("price", meta.get("price", ""))),
                "active", now(), now()
            ),
        )
        conn.commit()
        conn.close()


def update_order(order_id, status):
    with db_lock:
        conn = db()
        conn.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
            (status, now(), str(order_id)),
        )
        conn.commit()
        conn.close()


def stats():
    with db_lock:
        conn = db()
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        banned = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE banned=1"
        ).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status='active'"
        ).fetchone()["c"]
        finished = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status='finished'"
        ).fetchone()["c"]
        cancelled = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status='cancelled'"
        ).fetchone()["c"]
        conn.close()
    return users, banned, active, finished, cancelled


# -----------------------------
# User UI
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)

    if is_banned(update.effective_user.id):
        await update.message.reply_text("⛔ তোমার access বন্ধ করা হয়েছে।")
        return

    await update.message.reply_text(
        "👋 <b>Welcome!</b>\n\n"
        "Country → Product → Operator নির্বাচন করো।\n"
        "Purchase করার আগে confirmation দেখানো হবে।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🌍 Countries", callback_data="countries")
        ]]),
    )


async def show_countries(query):
    await query.answer()
    countries = await get_countries()

    if not countries:
        await query.edit_message_text("❌ কোনো country পাওয়া যায়নি।")
        return

    buttons = []
    for code, info in countries.items():
        name = (
            info.get("text_en") or info.get("name") or str(code).title()
            if isinstance(info, dict)
            else str(info or code).title()
        )
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
    products = await get_products(country)

    if not products:
        await query.edit_message_text("❌ কোনো product পাওয়া যায়নি।")
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
        InlineKeyboardButton("⬅️ Countries", callback_data="countries")
    ])

    await query.edit_message_text(
        f"🌍 <b>{country.title()}</b>\n\n📱 <b>Select Product</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_operators(query, country, product):
    await query.answer()
    operators = await get_operators(country, product)

    if not operators:
        await query.edit_message_text("❌ কোনো operator পাওয়া যায়নি।")
        return

    buttons = []
    for i, op in enumerate(operators):
        if not isinstance(op, dict):
            continue
        name = op.get("name", op.get("operator", "any"))
        price = op.get("customer_price", op.get("price", "-"))
        count = op.get("available_count", op.get("Qty", 0))
        buttons.append([
            InlineKeyboardButton(
                f"⚙️ {name} | ${price} | 📦 {count}",
                callback_data=f"op:{country}:{product}:{i}",
            )
        ])

    await query.edit_message_text(
        f"🌍 <b>{country}</b>\n📱 <b>{product}</b>\n\n⚙️ <b>Select Operator</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def confirm_purchase(query, context, country, product, index):
    operators = await get_operators(country, product)
    try:
        op = operators[int(index)]
    except Exception:
        await query.answer("Operator expired. আবার select করো.", show_alert=True)
        return

    if not isinstance(op, dict):
        await query.answer("Invalid operator.", show_alert=True)
        return

    operator = str(op.get("name", op.get("operator", "any")))
    price = op.get("customer_price", op.get("price", "-"))

    context.user_data["pending"] = {
        "country": country,
        "product": product,
        "operator": operator,
        "price": price,
    }

    await query.answer()
    await query.edit_message_text(
        "⚠️ <b>Confirm Purchase</b>\n\n"
        f"🌍 Country: <b>{country}</b>\n"
        f"📱 Product: <b>{product}</b>\n"
        f"⚙️ Operator: <b>{operator}</b>\n"
        f"💰 Price: <b>${price}</b>\n\n"
        "শুধু নিজের/অনুমোদিত testing-এর জন্য purchase করো।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="buy_confirm")],
            [InlineKeyboardButton("❌ Cancel", callback_data="pending_cancel")],
        ]),
    )


async def do_buy(query, context):
    pending = context.user_data.pop("pending", None)
    if not pending:
        await query.answer("Purchase session নেই.", show_alert=True)
        return

    await query.answer("Processing...")

    result = await buy_number(
        pending["country"], pending["product"], pending["operator"]
    )

    if not isinstance(result, dict) or str(result.get("status", "")).lower() != "success":
        msg = result.get("message", "Purchase failed.") if isinstance(result, dict) else "Purchase failed."
        await query.edit_message_text(f"❌ {msg}")
        return

    order_id = result.get("order_id")
    if not order_id:
        await query.edit_message_text("❌ API order_id দেয়নি।")
        return

    save_order(query.from_user.id, result, pending)

    context.user_data["active_order"] = {
        "order_id": str(order_id),
        "phone": str(result.get("phone", "")),
        "started": asyncio.get_running_loop().time(),
    }

    await query.edit_message_text(
        "✅ <b>Order Created</b>\n\n"
        f"🆔 Order: <code>{order_id}</code>\n"
        f"📞 Number: <code>{mask_phone(result.get('phone'))}</code>\n"
        f"💰 Price: <b>${result.get('price', pending['price'])}</b>\n\n"
        "⏳ Authorized testing-এর জন্য SMS status check করা হচ্ছে।\n"
        "Maximum wait: 120 seconds.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel Order", callback_data="order_cancel")
        ]]),
    )

    context.application.create_task(
        poll_order(context.application, query.from_user.id, context)
    )


async def poll_order(application, user_id, context):
    for _ in range(40):
        await asyncio.sleep(3)
        order = context.user_data.get("active_order")
        if not order or str(order["order_id"]) not in {
            str(order["order_id"])
        }:
            return

        try:
            result = await check_order(order["order_id"])
        except Exception:
            logger.exception("Order check failed")
            continue

        otp = otp_from(result)
        if otp:
            update_order(order["order_id"], "otp_received")
            await application.bot.send_message(
                chat_id=user_id,
                text=(
                    "📩 <b>OTP Received</b>\n\n"
                    f"🆔 Order: <code>{order['order_id']}</code>\n"
                    f"📞 Number: <code>{mask_phone(order['phone'])}</code>\n"
                    f"🔐 OTP: <code>{otp}</code>"
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Finish Order", callback_data="order_finish")
                ]]),
            )
            return

    order = context.user_data.get("active_order")
    if not order:
        return

    try:
        await cancel_order(order["order_id"])
    except Exception:
        logger.exception("Automatic cancellation failed")

    update_order(order["order_id"], "cancelled")
    context.user_data.pop("active_order", None)

    await application.bot.send_message(
        chat_id=user_id,
        text=(
            "⏱️ 120 seconds শেষ।\n"
            "📭 SMS/OTP পাওয়া যায়নি।\n"
            "❌ Cancel/refund request পাঠানো হয়েছে।"
        ),
    )


# -----------------------------
# Admin UI
# -----------------------------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)

    if not admin_only(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access নেই।")
        return

    await update.message.reply_text(
        "🛠️ <b>Admin Panel</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 Stats", callback_data="a:stats"),
                InlineKeyboardButton("👥 Users", callback_data="a:users"),
            ],
            [
                InlineKeyboardButton("📦 Orders", callback_data="a:orders"),
                InlineKeyboardButton("🔄 Refresh", callback_data="a:stats"),
            ],
            [
                InlineKeyboardButton("📢 Broadcast", callback_data="a:broadcast_help"),
            ],
        ]),
    )


async def admin_stats(query):
    users, banned, active, finished, cancelled = stats()
    await query.answer()
    await query.edit_message_text(
        "📊 <b>Statistics</b>\n\n"
        f"👥 Users: <b>{users}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n"
        f"🟢 Active orders: <b>{active}</b>\n"
        f"✅ Finished: <b>{finished}</b>\n"
        f"❌ Cancelled: <b>{cancelled}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Admin", callback_data="a:home")
        ]]),
    )


async def admin_users(query):
    with db_lock:
        conn = db()
        rows = conn.execute(
            """SELECT user_id, username, first_name, banned
               FROM users ORDER BY last_seen DESC LIMIT 15"""
        ).fetchall()
        conn.close()

    lines = ["👥 <b>Recent Users</b>\n"]
    for r in rows:
        name = r["first_name"] or r["username"] or "Unknown"
        flag = "🚫" if r["banned"] else "✅"
        lines.append(f"{flag} <code>{r['user_id']}</code> — {name}")

    await query.answer()
    await query.edit_message_text(
        "\n".join(lines) if rows else "No users yet.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Admin", callback_data="a:home")
        ]]),
    )


async def admin_orders(query):
    with db_lock:
        conn = db()
        rows = conn.execute(
            """SELECT order_id, telegram_user_id, product, status
               FROM orders ORDER BY id DESC LIMIT 15"""
        ).fetchall()
        conn.close()

    lines = ["📦 <b>Recent Orders</b>\n"]
    for r in rows:
        lines.append(
            f"🆔 <code>{r['order_id']}</code> | "
            f"👤 <code>{r['telegram_user_id']}</code> | "
            f"{r['product']} | {r['status']}"
        )

    await query.answer()
    await query.edit_message_text(
        "\n".join(lines) if rows else "No orders yet.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Admin", callback_data="a:home")
        ]]),
    )


async def admin_broadcast_help(query):
    await query.answer()
    await query.edit_message_text(
        "📢 <b>Broadcast</b>\n\n"
        "Use:\n<code>/broadcast তোমার message</code>\n\n"
        "এটা শুধু registered users-কে পাঠাবে।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Admin", callback_data="a:home")
        ]]),
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update.effective_user.id):
        await update.message.reply_text("⛔ Admin access নেই।")
        return

    message = update.message.text.partition(" ")[2].strip()
    if not message:
        await update.message.reply_text("ব্যবহার: /broadcast তোমার message")
        return

    with db_lock:
        conn = db()
        rows = conn.execute(
            "SELECT user_id FROM users WHERE banned=0"
        ).fetchall()
        conn.close()

    sent = 0
    for row in rows:
        try:
            await context.bot.send_message(row["user_id"], message)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    log_admin(update.effective_user.id, f"broadcast sent={sent}")
    await update.message.reply_text(f"📢 Broadcast complete: {sent} sent.")


# -----------------------------
# Callback router
# -----------------------------
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("a:"):
        if not admin_only(query.from_user.id):
            await query.answer("⛔ Admin access নেই.", show_alert=True)
            return

        if data == "a:home":
            await query.answer()
            await query.edit_message_text(
                "🛠️ <b>Admin Panel</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("📊 Stats", callback_data="a:stats"),
                        InlineKeyboardButton("👥 Users", callback_data="a:users"),
                    ],
                    [
                        InlineKeyboardButton("📦 Orders", callback_data="a:orders"),
                        InlineKeyboardButton("📢 Broadcast", callback_data="a:broadcast_help"),
                    ],
                ]),
            )
        elif data == "a:stats":
            await admin_stats(query)
        elif data == "a:users":
            await admin_users(query)
        elif data == "a:orders":
            await admin_orders(query)
        elif data == "a:broadcast_help":
            await admin_broadcast_help(query)
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
        return

    if data.startswith("op:"):
        parts = data.split(":", 3)
        if len(parts) == 4:
            await confirm_purchase(query, context, parts[1], parts[2], parts[3])
        return

    if data == "buy_confirm":
        await do_buy(query, context)
        return

    if data == "pending_cancel":
        context.user_data.pop("pending", None)
        await query.answer("Cancelled")
        await query.edit_message_text("❌ Purchase cancelled.")
        return

    if data == "order_cancel":
        order = context.user_data.get("active_order")
        if not order:
            await query.answer("No active order.", show_alert=True)
            return
        try:
            await cancel_order(order["order_id"])
        except Exception:
            logger.exception("Cancel failed")
        update_order(order["order_id"], "cancelled")
        context.user_data.pop("active_order", None)
        await query.answer("Cancelled")
        await query.edit_message_text("❌ Order cancelled/refund request sent.")
        return

    if data == "order_finish":
        order = context.user_data.get("active_order")
        if not order:
            await query.answer("No active order.", show_alert=True)
            return
        try:
            await finish_order(order["order_id"])
        except Exception:
            logger.exception("Finish failed")
        update_order(order["order_id"], "finished")
        context.user_data.pop("active_order", None)
        await query.answer("Finished")
        await query.edit_message_text("✅ Order finished.")
        return

    await query.answer()


async def error_handler(update, context):
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)


def main():
    init_db()
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_error_handler(error_handler)

    logger.info("Telegram bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
