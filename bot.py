"""
🤖 HR Leave Management Telegram Bot - Webhook Version
======================================================
برای هاست تلگرام VIPHost

Requirements:
    pip install python-telegram-bot==22.7 anthropic python-dotenv flask

فایل .env:
    TELEGRAM_TOKEN=توکن_ربات
    ANTHROPIC_API_KEY=کلید_انتروپیک
    ADMIN_TELEGRAM_ID=آیدی_عددی_ادمین
    WEBHOOK_URL=https://naser.s16.viptelbot.top
"""

import os
import sqlite3
import hashlib
import json
import asyncio
from datetime import datetime, date
from dotenv import load_dotenv
import anthropic
from flask import Flask, request as flask_request
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_TG_ID = int(os.getenv("ADMIN_TELEGRAM_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://naser.s16.viptelbot.top")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
flask_app = Flask(__name__)

# ─── Conversation States ───────────────────────────────────────────────────────
(
    AWAIT_USERNAME, AWAIT_PASSWORD,
    AWAIT_LEAVE_TYPE, AWAIT_LEAVE_START, AWAIT_LEAVE_END, AWAIT_LEAVE_DESC,
    MAIN_MENU,
) = range(7)

LEAVE_TYPES_FA = ["📅 استحقاقی", "🏥 استعلاجی", "💸 بدون حقوق", "🎯 اضطراری"]

# ─── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("leaves.db")
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            full_name  TEXT NOT NULL,
            tg_id      INTEGER,
            is_active  INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS leaves (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            leave_type  TEXT NOT NULL,
            start_date  TEXT NOT NULL,
            end_date    TEXT NOT NULL,
            description TEXT,
            status      TEXT DEFAULT 'pending',
            created_at  TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    hashed = hash_password("admin123")
    c.execute("""
        INSERT OR IGNORE INTO users (username, password, full_name)
        VALUES ('admin', ?, 'مدیر سیستم')
    """, (hashed,))
    conn.commit()
    conn.close()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_conn():
    return sqlite3.connect("leaves.db", check_same_thread=False)

def authenticate(username, password):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, full_name, is_active FROM users WHERE username=? AND password=?",
              (username, hash_password(password)))
    row = c.fetchone()
    conn.close()
    return row

def link_tg_id(user_id, tg_id):
    conn = get_conn()
    conn.execute("UPDATE users SET tg_id=? WHERE id=?", (tg_id, user_id))
    conn.commit()
    conn.close()

def get_user_leaves(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT leave_type, start_date, end_date, description, status, created_at
                 FROM leaves WHERE user_id=? ORDER BY created_at DESC LIMIT 10""", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def submit_leave(user_id, ltype, start, end, desc, auto):
    status = "auto" if auto else "pending"
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO leaves (user_id, leave_type, start_date, end_date, description, status) VALUES (?,?,?,?,?,?)",
              (user_id, ltype, start, end, desc, status))
    lid = c.lastrowid
    conn.commit()
    conn.close()
    return lid

def get_pending_leaves():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""SELECT l.id, u.full_name, l.leave_type, l.start_date, l.end_date, l.description
                 FROM leaves l JOIN users u ON l.user_id=u.id WHERE l.status='pending'""")
    rows = c.fetchall()
    conn.close()
    return rows

def update_leave_status(lid, status):
    conn = get_conn()
    conn.execute("UPDATE leaves SET status=?, reviewed_at=datetime('now') WHERE id=?", (status, lid))
    conn.commit()
    conn.close()

def get_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE is_active=1")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM leaves")
    total_leaves = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM leaves WHERE status='pending'")
    pending = c.fetchone()[0]
    conn.close()
    return total_users, total_leaves, pending

def add_user(username, password, full_name):
    try:
        conn = get_conn()
        conn.execute("INSERT INTO users (username, password, full_name) VALUES (?,?,?)",
                     (username, hash_password(password), full_name))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

# ─── AI ───────────────────────────────────────────────────────────────────────
def save_msg(user_id, role, content):
    conn = get_conn()
    conn.execute("INSERT INTO conversations (user_id, role, content) VALUES (?,?,?)",
                 (user_id, role, content))
    conn.commit()
    conn.close()

def get_history(user_id, limit=20):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT role, content FROM conversations WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
              (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))

def ai_reply(user_id, full_name, user_msg, leaves_summary):
    save_msg(user_id, "user", user_msg)
    history = get_history(user_id)
    system = f"""You are an HR assistant bot. Speak Persian or English based on user's language.
User: {full_name} | Today: {date.today()} | Their leaves: {leaves_summary}
Help with leave management. For new leave: use /newleave command."""
    messages = [{"role": r, "content": c} for r, c in history]
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=system,
        messages=messages,
    )
    reply = response.content[0].text
    save_msg(user_id, "assistant", reply)
    return reply

# ─── Keyboards ────────────────────────────────────────────────────────────────
def main_menu_kb(is_admin=False):
    buttons = [
        ["📝 ثبت مرخصی", "📋 مرخصی‌هام"],
        ["🤖 دستیار هوشمند"],
    ]
    if is_admin:
        buttons.append(["⚙️ پنل ادمین"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def leave_type_kb():
    return ReplyKeyboardMarkup([[t] for t in LEAVE_TYPES_FA] + [["🔙 بازگشت"]], resize_keyboard=True)

# ─── Handlers ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "👋 سلام! به ربات مدیریت مرخصی خوش آمدید.\n\n🔐 نام کاربری خود را وارد کنید:"
    )
    return AWAIT_USERNAME

async def get_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["username"] = update.message.text.strip()
    await update.message.reply_text("🔑 رمز عبور:")
    return AWAIT_PASSWORD

async def get_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = authenticate(ctx.user_data.get("username"), update.message.text.strip())
    if not result:
        await update.message.reply_text("❌ نام کاربری یا رمز عبور اشتباه است.\n/start")
        return ConversationHandler.END
    uid, full_name, is_active = result
    if not is_active:
        await update.message.reply_text("⛔ حساب شما غیرفعال است.")
        return ConversationHandler.END
    ctx.user_data.update({"uid": uid, "full_name": full_name,
                          "is_admin": update.effective_user.id == ADMIN_TG_ID})
    link_tg_id(uid, update.effective_user.id)
    await update.message.reply_text(f"✅ خوش آمدید، {full_name}!",
                                    reply_markup=main_menu_kb(ctx.user_data["is_admin"]))
    return MAIN_MENU

async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "uid" not in ctx.user_data:
        await update.message.reply_text("لطفاً ابتدا وارد شوید — /start")
        return ConversationHandler.END
    text = update.message.text

    if "ثبت مرخصی" in text:
        await update.message.reply_text("نوع مرخصی را انتخاب کنید:", reply_markup=leave_type_kb())
        return AWAIT_LEAVE_TYPE
    elif "مرخصی‌هام" in text:
        await show_my_leaves(update, ctx)
        return MAIN_MENU
    elif "دستیار" in text:
        ctx.user_data["ai_mode"] = True
        await update.message.reply_text("🤖 دستیار هوشمند آماده است. سوال بپرسید!\n(برای بازگشت: /start)")
        return MAIN_MENU
    elif "ادمین" in text and ctx.user_data.get("is_admin"):
        await show_admin_panel(update, ctx)
        return MAIN_MENU
    elif ctx.user_data.get("ai_mode"):
        uid = ctx.user_data["uid"]
        leaves = get_user_leaves(uid)
        reply = ai_reply(uid, ctx.user_data["full_name"], text,
                         json.dumps(leaves, ensure_ascii=False) if leaves else "هنوز مرخصی ندارد")
        await update.message.reply_text(f"🤖 {reply}")
        return MAIN_MENU
    return MAIN_MENU

async def show_my_leaves(update, ctx):
    leaves = get_user_leaves(ctx.user_data["uid"])
    if not leaves:
        await update.message.reply_text("📭 هنوز مرخصی ثبت نکرده‌اید.")
        return
    STATUS = {"pending": "⏳", "approved": "✅", "rejected": "❌", "auto": "🤖"}
    lines = ["📋 مرخصی‌های شما:\n"]
    for ltype, s, e, desc, status, _ in leaves:
        lines.append(f"{STATUS.get(status,'❓')} {ltype}\n   📆 {s} → {e}\n   💬 {desc or '—'}\n")
    await update.message.reply_text("\n".join(lines))

async def get_leave_type(update, ctx):
    t = update.message.text
    if "بازگشت" in t:
        await update.message.reply_text("🔙", reply_markup=main_menu_kb(ctx.user_data.get("is_admin")))
        return MAIN_MENU
    ctx.user_data["leave_type"] = t
    await update.message.reply_text("📅 تاریخ شروع (مثال: 2024-06-01):")
    return AWAIT_LEAVE_START

async def get_leave_start(update, ctx):
    try:
        datetime.strptime(update.message.text.strip(), "%Y-%m-%d")
        ctx.user_data["leave_start"] = update.message.text.strip()
        await update.message.reply_text("📅 تاریخ پایان:")
        return AWAIT_LEAVE_END
    except ValueError:
        await update.message.reply_text("❌ فرمت اشتباه. مثال: 2024-06-01")
        return AWAIT_LEAVE_START

async def get_leave_end(update, ctx):
    try:
        end = datetime.strptime(update.message.text.strip(), "%Y-%m-%d")
        start = datetime.strptime(ctx.user_data["leave_start"], "%Y-%m-%d")
        if end < start:
            await update.message.reply_text("❌ تاریخ پایان باید بعد از شروع باشد.")
            return AWAIT_LEAVE_END
        ctx.user_data["leave_end"] = update.message.text.strip()
        await update.message.reply_text("💬 توضیحات (یا 'skip' برای رد کردن):")
        return AWAIT_LEAVE_DESC
    except ValueError:
        await update.message.reply_text("❌ فرمت اشتباه. مثال: 2024-06-01")
        return AWAIT_LEAVE_END

async def get_leave_desc(update, ctx):
    desc = update.message.text.strip()
    if desc.lower() == "skip":
        desc = ""
    uid   = ctx.user_data["uid"]
    ltype = ctx.user_data["leave_type"]
    start = ctx.user_data["leave_start"]
    end   = ctx.user_data["leave_end"]
    delta = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    auto  = delta <= 2
    lid   = submit_leave(uid, ltype, start, end, desc, auto)
    status_msg = "🤖 خودکار تایید شد." if auto else "⏳ منتظر تایید ادمین."
    await update.message.reply_text(
        f"✅ مرخصی ثبت شد!\n📌 {ltype}\n📆 {start} → {end}\n{status_msg}",
        reply_markup=main_menu_kb(ctx.user_data.get("is_admin"))
    )
    if not auto:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تایید", callback_data=f"approve_{lid}"),
            InlineKeyboardButton("❌ رد",    callback_data=f"reject_{lid}"),
        ]])
        try:
            await ctx.bot.send_message(ADMIN_TG_ID,
                f"🔔 درخواست مرخصی:\n👤 {ctx.user_data['full_name']}\n🗂 {ltype}\n📆 {start} → {end}\n💬 {desc or '—'}",
                reply_markup=kb)
        except Exception:
            pass
    return MAIN_MENU

async def show_admin_panel(update, ctx):
    total_users, total_leaves, pending = get_stats()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ درخواست‌های در انتظار", callback_data="admin_pending")],
        [InlineKeyboardButton("👥 لیست پرسنل", callback_data="admin_users")],
    ])
    await update.message.reply_text(
        f"⚙️ پنل ادمین\n\n👥 پرسنل: {total_users}\n📋 کل مرخصی‌ها: {total_leaves}\n⏳ در انتظار: {pending}",
        reply_markup=kb
    )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "admin_pending":
        rows = get_pending_leaves()
        if not rows:
            await query.edit_message_text("✅ هیچ درخواستی در انتظار نیست.")
            return
        for row in rows:
            lid, name, ltype, s, e, desc = row
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تایید", callback_data=f"approve_{lid}"),
                InlineKeyboardButton("❌ رد",    callback_data=f"reject_{lid}"),
            ]])
            await ctx.bot.send_message(query.message.chat_id,
                f"👤 {name}\n🗂 {ltype}\n📆 {s} → {e}\n💬 {desc or '—'}", reply_markup=kb)
    elif data == "admin_users":
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT username, full_name, is_active FROM users")
        users = c.fetchall()
        conn.close()
        lines = ["👥 لیست پرسنل:\n"]
        for uname, fname, active in users:
            lines.append(f"{'✅' if active else '🚫'} {fname} (@{uname})")
        await query.edit_message_text("\n".join(lines))
    elif data.startswith("approve_"):
        update_leave_status(int(data.split("_")[1]), "approved")
        await query.edit_message_text("✅ مرخصی تایید شد.")
    elif data.startswith("reject_"):
        update_leave_status(int(data.split("_")[1]), "rejected")
        await query.edit_message_text("❌ مرخصی رد شد.")

# ─── Application Setup ────────────────────────────────────────────────────────
application = None

def get_or_create_loop():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Loop is closed")
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

def build_application():
    global application
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAIT_USERNAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_username)],
            AWAIT_PASSWORD:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
            MAIN_MENU:        [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
            AWAIT_LEAVE_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_leave_type)],
            AWAIT_LEAVE_START:[MessageHandler(filters.TEXT & ~filters.COMMAND, get_leave_start)],
            AWAIT_LEAVE_END:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_leave_end)],
            AWAIT_LEAVE_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_leave_desc)],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback_handler))
    application = app
    return app

# ─── Flask Webhook ────────────────────────────────────────────────────────────
@flask_app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    data = flask_request.get_json()
    update = Update.de_json(data, application.bot)
    loop = get_or_create_loop()
    loop.run_until_complete(application.process_update(update))
    return "OK"

@flask_app.route("/")
def index():
    return "✅ Bot is running!"

@flask_app.route("/set_webhook")
def set_webhook():
    url = f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
    loop = get_or_create_loop()
    result = loop.run_until_complete(application.bot.set_webhook(url))
    return f"Webhook set: {result} → {url}"

# ─── Initialize ───────────────────────────────────────────────────────────────
build_application()
loop = get_or_create_loop()
loop.run_until_complete(application.initialize())

if __name__ == "__main__":
    print("🤖 Bot running with webhook...")
    flask_app.run(host="0.0.0.0", port=5000)
