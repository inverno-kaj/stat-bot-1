import os
import sqlite3
import shutil
from datetime import datetime, timezone, timedelta
from html import escape
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from datetime import datetime

DB_PATH = os.getenv("DB_PATH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

# Для України/Києва. Якщо сервер в іншій TZ — статистика все одно буде по UTC+3.
LOCAL_TZ = timezone(timedelta(hours=3))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            thread_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_period ON messages(chat_id, thread_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON messages(chat_id, thread_id, user_id)")


def now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def period_start(period: str) -> str | None:
    now = datetime.now(LOCAL_TZ)
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    if period == "week":
        start = now - timedelta(days=now.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    if period == "all":
        return None
    return None


def period_label(period: str) -> str:
    return {
        "day": "сьогодні",
        "week": "цього тижня",
        "month": "цього місяця",
        "all": "за весь час",
    }.get(period, period)


def get_thread_id(update: Update) -> int:
    # У гілках Telegram Forum Topics тут буде реальний message_thread_id.
    # У звичайному чаті або General — ставимо 0.
    msg = update.effective_message
    return msg.message_thread_id or 0


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or user.is_bot or not chat:
        return

    # Не рахуємо команди як звичайні повідомлення
    if msg.text and msg.text.startswith("/"):
        return

    with db() as conn:
        conn.execute(
            """
            INSERT INTO messages(chat_id, thread_id, user_id, username, first_name, last_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat.id,
                get_thread_id(update),
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                now_iso(),
            ),
        )


def build_top(chat_id: int, thread_id: int | None, period: str):
    start = period_start(period)
    where = ["chat_id = ?"]
    params: list = [chat_id]

    if thread_id is not None:
        where.append("thread_id = ?")
        params.append(thread_id)

    if start:
        where.append("created_at >= ?")
        params.append(start)

    query = f"""
        SELECT user_id, username, first_name, last_name, COUNT(*) AS count
        FROM messages
        WHERE {' AND '.join(where)}
        GROUP BY user_id
        ORDER BY count DESC
    """

    with db() as conn:
        return conn.execute(query, params).fetchall()

def format_user(row: sqlite3.Row) -> str:
    name = " ".join(filter(None, [row["first_name"], row["last_name"]])).strip()
    if row["username"]:
        name = f"@{row['username']}"
    if not name:
        name = f"ID {row['user_id']}"
    return escape(name)


def format_top(rows, title: str) -> str:
    if not rows:
        return f"<b>{escape(title)}</b>\n\nПоки немає повідомлень у цій статистиці."
    lines = [f"<b>{escape(title)}</b>", ""]
    for i, row in enumerate(rows, start=1):
        lines.append(f"{i}. {format_user(row)} — <b>{row['count']}</b>")
    return "\n".join(lines)

async def send_long_html(message, text: str) -> None:
    max_len = 3900
    parts = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            parts.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line

    if current:
        parts.append(current)

    for part in parts:
        await message.reply_html(part)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привіт! Я рахую повідомлення користувачів окремо по гілках чату.\n\n"
        "Команди:\n"
        "/top_day — топ за день у всьому чаті\n"
        "/top_week — топ за тиждень у всьому чаті\n"
        "/top_month — топ за місяць у всьому чаті\n"
        "/top_all — топ за весь час у всьому чаті\n\n"
        "/thread_day — топ за день у цій гілці\n"
        "/thread_week — топ за тиждень у цій гілці\n"
        "/thread_month — топ за місяць у цій гілці\n"
        "/thread_all — топ за весь час у цій гілці\n\n"
        "/me — моя статистика в цій гілці\n"
        "/help — допомога"
    )
    await update.effective_message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str) -> None:
    chat = update.effective_chat
    rows = build_top(chat.id, None, period)
    await send_long_html(
        update.effective_message,
        format_top(rows, f"Топ активності {period_label(period)} — {thread_name}")
    )


async def thread_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str) -> None:
    chat = update.effective_chat
    thread_id = get_thread_id(update)
    rows = build_top(chat.id, thread_id, period)
    thread_name = "General" if thread_id == 0 else f"гілка #{thread_id}"
    await send_long_html(
        update.effective_message,
        format_top(rows, f"Топ активності {period_label(period)} — {thread_name}")
    )


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    thread_id = get_thread_id(update)

    result = {}
    with db() as conn:
        for p in ["day", "week", "month", "all"]:
            where = "chat_id = ? AND thread_id = ? AND user_id = ?"
            params: list = [chat.id, thread_id, user.id]
            start = period_start(p)
            if start:
                where += " AND created_at >= ?"
                params.append(start)
            count = conn.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", params).fetchone()[0]
            result[p] = count

    thread_name = "General" if thread_id == 0 else f"гілка #{thread_id}"
    text = (
        f"<b>Твоя статистика — {escape(thread_name)}</b>\n\n"
        f"Сьогодні: <b>{result['day']}</b>\n"
        f"Тиждень: <b>{result['week']}</b>\n"
        f"Місяць: <b>{result['month']}</b>\n"
        f"Загалом: <b>{result['all']}</b>"
    )
    await update.effective_message.reply_html(text)


ADMINS = {
    781632572,
    951531976
}

async def backup_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    # Тільки в особистих повідомленнях
    if chat.type != "private":
        return

    # Тільки для адміністраторів
    if not user or user.id not in ADMINS:
        await update.effective_message.reply_text(
            "⛔ У вас немає доступу до цієї команди."
        )
        return

    if not os.path.exists(DB_PATH):
        await update.effective_message.reply_text(
            "❌ Файл бази не знайдено."
        )
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = f"backup_stats_{timestamp}.db"

    shutil.copy2(DB_PATH, backup_path)

    try:
        with open(backup_path, "rb") as file:
            await update.effective_message.reply_document(
                document=file,
                filename=f"stats_backup_{timestamp}.db",
                caption=(
                    f"📦 Резервна копія бази\n"
                    f"Розмір: {os.path.getsize(DB_PATH)} байт"
                )
            )
    finally:
        if os.path.exists(backup_path):
            os.remove(backup_path)
            
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не знайдено BOT_TOKEN. Додай токен у .env або змінну середовища BOT_TOKEN.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("backup_db", backup_db))

    app.add_handler(CommandHandler("top_day", lambda u, c: top_cmd(u, c, "day")))
    app.add_handler(CommandHandler("top_week", lambda u, c: top_cmd(u, c, "week")))
    app.add_handler(CommandHandler("top_month", lambda u, c: top_cmd(u, c, "month")))
    app.add_handler(CommandHandler("top_all", lambda u, c: top_cmd(u, c, "all")))

    app.add_handler(CommandHandler("thread_day", lambda u, c: thread_cmd(u, c, "day")))
    app.add_handler(CommandHandler("thread_week", lambda u, c: thread_cmd(u, c, "week")))
    app.add_handler(CommandHandler("thread_month", lambda u, c: thread_cmd(u, c, "month")))
    app.add_handler(CommandHandler("thread_all", lambda u, c: thread_cmd(u, c, "all")))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_message))

    print("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
