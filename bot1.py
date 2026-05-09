"""
🤖 HR Leave Management Telegram Bot
=====================================
Requirements:
    pip install python-telegram-bot==20.7 anthropic sqlite3 python-dotenv

Setup:
    1. Create .env file with the variables below
    2. Run: python bot.py

.env file:
    TELEGRAM_TOKEN=your_bot_token_from_botfather
    ANTHROPIC_API_KEY=your_anthropic_api_key
    ADMIN_TELEGRAM_ID=your_telegram_numeric_id
"""

import os
import sqlite3
import hashlib
import json
from datetime import datetime, date
from dotenv import load_dotenv
import anthropic
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
import telegram

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_TG_ID       = int(os.getenv("ADMIN_TELEGRAM_ID"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── Conversation States ────────────────────────────────────────────────────────
(
    AWAIT_USERNAME, AWAIT_PASSWORD,
    AWAIT_LEAVE_TYPE, AWAIT_LEAVE_START, AWAIT_LEAVE_END, AWAIT_LEAVE_DESC,
    AWAIT_NEW_USER, AWAIT_NEW_PASS, AWAIT_NEW_NAME,
    MAIN_MENU,
) = range(10)

LEAVE_TYPES = {
    "fa": ["📅 استحقاقی", "🏥 استعلاجی", "💸 بدون حقوق", "🎯 اضطراری"],
    "en": ["📅 Annual",   "🏥 Sick",      "💸 Unpaid",    "🎯 Emergency"],
}

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
            status      TEXT DEFAULT 'pending',  -- pending/approved/rejected/auto
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

    # Create default admin user if not exists
    hashed = hash_password("admin123")
    c.execute("""
        INSERT OR IGNORE INTO users (username, password, full_name)
        VALUES ('admin', ?, 'مدیر سیستم / System Admin')
    """, (hashed,))
    conn.commit()
    conn.close()

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def get_conn():
    return sqlite3.connect("leaves.db", check_same_thread=False)

# ─── DB Helpers ────────────────────────────────────────────────────────────────
def authenticate(username: str, password: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, full_name, is_active FROM users WHERE username=? AND password=?",
        (username, hash_password(password))
    )
    row = c.fetchone()
    conn.close()
    return row  # (id, full_name, is_active) or None

def link_tg_id(user_id: int, tg_id: int):
    conn = get_conn()
    conn.execute("UPDATE users SET tg_id=? WHERE id=?", (tg_id, user_id))
    conn.commit()
    conn.close()

def get_user_leaves(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT leave_type, start_date, end_date, description, status, created_at
        FROM leaves WHERE user_id=? ORDER BY created_at DESC LIMIT 10
    """, (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def submit_leave(user_id: int, ltype: str, start: str, end: str, desc: str, auto: bool):
    status = "auto" if auto else "pending"
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO leaves (user_id, leave_type, start_date, end_date, description, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, ltype, start, end, desc, status))
    leave_id = c.lastrowid
    conn.commit()
    conn.close()
    return leave_id

def get_pending_leaves():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT l.id, u.full_name, l.leave_type, l.start_date, l.end_date,
               l.description, l.created_at
        FROM leaves l JOIN users u ON l.user_id = u.id
        WHERE l.status='pending' ORDER BY l.created_at
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def update_leave_status(leave_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE leaves SET status=?, reviewed_at=datetime('now') WHERE id=?",
        (status, leave_id)
    )
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id, username, full_name, is_active FROM users ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows

def add_user(username: str, password: str, full_name: str):
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO users (username, password, full_name) VALUES (?,?,?)",
            (username, hash_password(password), full_name)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

def get_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE is_active=1")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM leaves")
    total_leaves = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM leaves WHERE status='pending'")
    pending = c.fetchone()[0]
    c.execute("""
        SELECT leave_type, COUNT(*) FROM leaves
        WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
        GROUP BY leave_type
    """)
    monthly = c.fetchall()
    conn.close()
    return total_users, total_leaves, pending, monthly

# ─── AI Memory ─────────────────────────────────────────────────────────────────
def save_msg(user_id: int, role: str, content: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO conversations (user_id, role, content) VALUES (?,?,?)",
        (user_id, role, content)
    )
    conn.commit()
    conn.close()

def get_history(user_id: int, limit: int = 20):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT role, content FROM conversations
        WHERE user_id=? ORDER BY created_at DESC LIMIT ?
    """, (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))

def ai_reply(user_id: int, full_name: str, user_msg: str, leaves_summary: str) -> str:
    save_msg(user_id, "user", user_msg)
    history = get_history(user_id)

    system = f"""You are an HR assistant bot for a company. You speak both Persian (Farsi) and English.
Always respond in the same language the user writes in.
Current user: {full_name} (ID: {user_id})
Their recent leaves: {leaves_summary}
Today: {date.today().isoformat()}

Help users with leave management, HR questions, and company policies.
Be friendly, professional, and concise. If the user asks about their leaves, use the data above.
For leave submission, guide them to use the /newleave command."""

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

# ─── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_kb(is_admin: bool = False):
    buttons = [
        ["📝 ثبت مرخصی / New Leave", "📋 مرخصی‌هام / My Leaves"],
        ["🤖 دستیار هوشمند / AI Assistant"],
    ]
    if is_admin:
        buttons.append(["⚙️ پنل ادمین / Admin Panel"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def leave_type_kb():
    buttons = [[t] for t in LEAVE_TYPES["fa"]]
    buttons.append(["🔙 بازگشت / Back"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ─── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "👋 سلام! به ربات مدیریت مرخصی خوش آمدید.\n"
        "Hello! Welcome to the Leave Management Bot.\n\n"
        "🔐 لطفاً نام کاربری خود را وارد کنید:\n"
        "Please enter your username:"
    )
    return AWAIT_USERNAME

async def get_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["username"] = update.message.text.strip()
    await update.message.reply_text(
        "🔑 رمز عبور / Password:"
    )
    return AWAIT_PASSWORD

async def get_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = ctx.user_data.get("username")
    password = update.message.text.strip()
    result = authenticate(username, password)

    if not result:
        await update.message.reply_text(
            "❌ نام کاربری یا رمز عبور اشتباه است.\n"
            "Invalid username or password.\n\n"
            "دوباره تلاش کنید / Try again — /start"
        )
        return ConversationHandler.END

    uid, full_name, is_active = result
    if not is_active:
        await update.message.reply_text("⛔ حساب شما غیرفعال است. / Account disabled.")
        return ConversationHandler.END

    ctx.user_data["uid"]       = uid
    ctx.user_data["full_name"] = full_name
    ctx.user_data["is_admin"]  = (update.effective_user.id == ADMIN_TG_ID)
    link_tg_id(uid, update.effective_user.id)

    await update.message.reply_text(
        f"✅ خوش آمدید، {full_name}!\nWelcome, {full_name}!",
        reply_markup=main_menu_kb(ctx.user_data["is_admin"])
    )
    return MAIN_MENU

# ── Main Menu Router ───────────────────────────────────────────────────────────
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "uid" not in ctx.user_data:
        await update.message.reply_text("لطفاً ابتدا وارد شوید / Please login first — /start")
        return ConversationHandler.END

    text = update.message.text

    if "ثبت مرخصی" in text or "New Leave" in text:
        await update.message.reply_text(
            "📅 نوع مرخصی را انتخاب کنید / Select leave type:",
            reply_markup=leave_type_kb()
        )
        return AWAIT_LEAVE_TYPE

    elif "مرخصی‌هام" in text or "My Leaves" in text:
        await show_my_leaves(update, ctx)
        return MAIN_MENU

    elif "دستیار" in text or "AI" in text:
        await update.message.reply_text(
            "🤖 دستیار هوشمند فعال است. هر سوالی دارید بپرسید.\n"
            "AI Assistant is ready. Ask me anything!\n\n"
            "(برای بازگشت: /menu)"
        )
        ctx.user_data["ai_mode"] = True
        return MAIN_MENU

    elif ("ادمین" in text or "Admin" in text) and ctx.user_data.get("is_admin"):
        await show_admin_panel(update, ctx)
        return MAIN_MENU

    elif ctx.user_data.get("ai_mode"):
        uid       = ctx.user_data["uid"]
        full_name = ctx.user_data["full_name"]
        leaves    = get_user_leaves(uid)
        leaves_summary = json.dumps(leaves, ensure_ascii=False) if leaves else "No leaves yet"
        reply = ai_reply(uid, full_name, text, leaves_summary)
        await update.message.reply_text(f"🤖 {reply}")
        return MAIN_MENU

    return MAIN_MENU

async def show_my_leaves(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    leaves = get_user_leaves(ctx.user_data["uid"])
    if not leaves:
        await update.message.reply_text(
            "📭 هنوز مرخصی ثبت نکرده‌اید.\nNo leaves registered yet."
        )
        return

    STATUS_EMOJI = {"pending": "⏳", "approved": "✅", "rejected": "❌", "auto": "🤖"}
    lines = ["📋 **مرخصی‌های شما / Your Leaves:**\n"]
    for ltype, s, e, desc, status, created in leaves:
        emoji = STATUS_EMOJI.get(status, "❓")
        lines.append(f"{emoji} {ltype}\n   📆 {s} ← {e}\n   💬 {desc or '—'}\n   📌 {status}\n")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Leave Submission Flow ──────────────────────────────────────────────────────
async def get_leave_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    if "بازگشت" in t or "Back" in t:
        await update.message.reply_text("🔙", reply_markup=main_menu_kb(ctx.user_data.get("is_admin")))
        return MAIN_MENU
    ctx.user_data["leave_type"] = t
    await update.message.reply_text(
        "📅 تاریخ شروع مرخصی را وارد کنید (مثال: 2024-06-01):\n"
        "Enter start date (e.g. 2024-06-01):"
    )
    return AWAIT_LEAVE_START

async def get_leave_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text.strip(), "%Y-%m-%d")
        ctx.user_data["leave_start"] = update.message.text.strip()
        await update.message.reply_text(
            "📅 تاریخ پایان مرخصی:\nEnter end date:"
        )
        return AWAIT_LEAVE_END
    except ValueError:
        await update.message.reply_text("❌ فرمت تاریخ اشتباه است. مثال: 2024-06-01")
        return AWAIT_LEAVE_START

async def get_leave_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        end = datetime.strptime(update.message.text.strip(), "%Y-%m-%d")
        start = datetime.strptime(ctx.user_data["leave_start"], "%Y-%m-%d")
        if end < start:
            await update.message.reply_text("❌ تاریخ پایان باید بعد از شروع باشد.")
            return AWAIT_LEAVE_END
        ctx.user_data["leave_end"] = update.message.text.strip()
        await update.message.reply_text(
            "💬 توضیحات (اختیاری) / Description (optional):\n"
            "(برای رد کردن 'skip' بنویسید / type 'skip' to skip)"
        )
        return AWAIT_LEAVE_DESC
    except ValueError:
        await update.message.reply_text("❌ فرمت تاریخ اشتباه. مثال: 2024-06-01")
        return AWAIT_LEAVE_END

async def get_leave_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    if desc.lower() == "skip":
        desc = ""

    uid   = ctx.user_data["uid"]
    ltype = ctx.user_data["leave_type"]
    start = ctx.user_data["leave_start"]
    end   = ctx.user_data["leave_end"]

    # Auto-approve short leaves (≤2 days); otherwise pending
    delta = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    auto  = delta <= 2

    leave_id = submit_leave(uid, ltype, start, end, desc, auto)

    status_msg = (
        "🤖 مرخصی به‌صورت خودکار تایید شد (تا ۲ روز).\nAuto-approved (≤2 days)."
        if auto else
        "⏳ درخواست شما ثبت و منتظر تایید ادمین است.\nPending admin approval."
    )

    await update.message.reply_text(
        f"✅ مرخصی ثبت شد! / Leave submitted!\n\n"
        f"📌 نوع: {ltype}\n📆 {start} → {end}\n\n{status_msg}",
        reply_markup=main_menu_kb(ctx.user_data.get("is_admin"))
    )

    # Notify admin for pending leaves
    if not auto:
        name = ctx.user_data["full_name"]
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تایید / Approve", callback_data=f"approve_{leave_id}"),
                InlineKeyboardButton("❌ رد / Reject",    callback_data=f"reject_{leave_id}"),
            ]
        ])
        try:
            await ctx.bot.send_message(
                ADMIN_TG_ID,
                f"🔔 درخواست مرخصی جدید:\n👤 {name}\n🗂 {ltype}\n📆 {start} → {end}\n💬 {desc or '—'}",
                reply_markup=kb
            )
        except Exception:
            pass

    return MAIN_MENU

# ── Admin Panel ────────────────────────────────────────────────────────────────
async def show_admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total_users, total_leaves, pending, monthly = get_stats()
    monthly_str = "\n".join([f"  • {t}: {c}" for t, c in monthly]) or "  (هیچ)"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ درخواست‌های در انتظار", callback_data="admin_pending")],
        [InlineKeyboardButton("👥 لیست پرسنل", callback_data="admin_users")],
        [InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="admin_adduser")],
    ])

    await update.message.reply_text(
        f"⚙️ **پنل ادمین / Admin Panel**\n\n"
        f"👥 پرسنل فعال: {total_users}\n"
        f"📋 کل مرخصی‌ها: {total_leaves}\n"
        f"⏳ در انتظار تایید: {pending}\n\n"
        f"📊 این ماه:\n{monthly_str}",
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "admin_pending":
        rows = get_pending_leaves()
        if not rows:
            await query.edit_message_text("✅ هیچ درخواستی در انتظار نیست.\nNo pending requests.")
            return
        for row in rows:
            lid, name, ltype, s, e, desc, created = row
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تایید", callback_data=f"approve_{lid}"),
                InlineKeyboardButton("❌ رد",    callback_data=f"reject_{lid}"),
            ]])
            await ctx.bot.send_message(
                query.message.chat_id,
                f"👤 {name}\n🗂 {ltype}\n📆 {s} → {e}\n💬 {desc or '—'}\n📌 ID: {lid}",
                reply_markup=kb
            )

    elif data == "admin_users":
        users = get_all_users()
        lines = ["👥 **لیست پرسنل:**\n"]
        for uid, uname, fname, active in users:
            icon = "✅" if active else "🚫"
            lines.append(f"{icon} {fname} (@{uname})")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif data == "admin_adduser":
        ctx.user_data["adding_user"] = True
        await ctx.bot.send_message(
            query.message.chat_id,
            "➕ نام کاربری جدید را وارد کنید:\nEnter new username:"
        )

    elif data.startswith("approve_"):
        lid = int(data.split("_")[1])
        update_leave_status(lid, "approved")
        await query.edit_message_text(f"✅ مرخصی #{lid} تایید شد.")

    elif data.startswith("reject_"):
        lid = int(data.split("_")[1])
        update_leave_status(lid, "rejected")
        await query.edit_message_text(f"❌ مرخصی #{lid} رد شد.")

# ── Add User Flow (Admin) ──────────────────────────────────────────────────────
async def admin_add_user_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("adding_user"):
        return

    step = ctx.user_data.get("add_step", 0)

    if step == 0:
        ctx.user_data["new_username"] = update.message.text.strip()
        ctx.user_data["add_step"] = 1
        await update.message.reply_text("🔑 رمز عبور برای این کاربر:\nPassword for this user:")

    elif step == 1:
        ctx.user_data["new_password"] = update.message.text.strip()
        ctx.user_data["add_step"] = 2
        await update.message.reply_text("👤 نام کامل پرسنل:\nFull name:")

    elif step == 2:
        full_name = update.message.text.strip()
        ok = add_user(ctx.user_data["new_username"], ctx.user_data["new_password"], full_name)
        ctx.user_data.pop("adding_user", None)
        ctx.user_data.pop("add_step", None)
        if ok:
            await update.message.reply_text(f"✅ کاربر '{full_name}' اضافه شد!")
        else:
            await update.message.reply_text("❌ این نام کاربری قبلاً وجود دارد.")

# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_db()

    # اگه تلگرام دسکتاپت پروکسی داره، اینجا تنظیم کن
    # مثال: proxy = "socks5://127.0.0.1:10808"
    # مثال: proxy = "http://127.0.0.1:10809"
    proxy = None  # WireGuard کل ترافیک رو مدیریت میکنه

    builder = Application.builder().token(TELEGRAM_TOKEN)
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    # استفاده از سرور محلی تلگرام برای دور زدن محدودیت‌ها
    builder = builder.base_url("https://api.telegram.org/bot")
    app = builder.build()

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_user_flow))

    print("🤖 Bot is running...")
    print(f"python-telegram-bot version: {telegram.__version__}")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
