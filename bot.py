import os
import sqlite3
import shutil
import json
import gspread

from datetime import datetime, timezone, timedelta, date, time
from html import escape

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from google.oauth2.service_account import Credentials


DB_PATH = os.getenv("DB_PATH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

SPREADSHEET_ID = "1wvNuyiW0d-3imx8hXmEB-gokiScxojGUv7tn4SkdS0o"
LOCAL_TZ = timezone(timedelta(hours=3))


if DB_PATH:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


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

    return None


def period_label(period: str) -> str:
    return {
        "day": "сьогодні",
        "week": "цього тижня",
        "month": "цього місяця",
        "all": "за весь час",
    }.get(period, period)


def get_thread_id(update: Update) -> int:
    msg = update.effective_message
    return msg.message_thread_id or 0


async def safe_send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat = update.effective_chat
    msg = update.effective_message

    if not chat:
        return

    await context.bot.send_message(
        chat_id=chat.id,
        message_thread_id=msg.message_thread_id if msg and msg.message_thread_id else None,
        text=text
    )


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not msg or not user or user.is_bot or not chat:
        return

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
    params = [chat_id]

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
        "/чистка — внести чистку за поточну неділю\n"
        "/оновити_чистку 07.06.2026 — оновити конкретний день чистки\n"
        "/update_clean 07.06.2026 — те саме латиницею\n\n"
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
        format_top(rows, f"Топ активності {period_label(period)} — весь чат")
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
            params = [chat.id, thread_id, user.id]

            start = period_start(p)

            if start:
                where += " AND created_at >= ?"
                params.append(start)

            count = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {where}",
                params
            ).fetchone()[0]

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
    951531976,
}


async def backup_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if chat.type != "private":
        return

    if not user or user.id not in ADMINS:
        await update.effective_message.reply_text("⛔ У вас немає доступу до цієї команди.")
        return

    if not os.path.exists(DB_PATH):
        await update.effective_message.reply_text("❌ Файл бази не знайдено.")
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


def get_worksheet(name: str):
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if not creds_json:
        raise RuntimeError("Не знайдено GOOGLE_CREDENTIALS_JSON")

    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    return client.open_by_key(SPREADSHEET_ID).worksheet(name)


def normalize_text(value) -> str:
    return str(value or "").strip().lower()


def parse_date(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    value = str(value).strip()

    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    return None


def current_sunday() -> date:
    today = datetime.now(LOCAL_TZ).date()
    return today - timedelta(days=(today.weekday() + 1) % 7)


def week_range_for_clean_date(clean_date: date):
    start_date = clean_date - timedelta(days=clean_date.weekday())
    end_date = start_date + timedelta(days=7)

    start_dt = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
    end_dt = datetime.combine(end_date, time.min, tzinfo=LOCAL_TZ)

    return (
        start_dt.isoformat(timespec="seconds"),
        end_dt.isoformat(timespec="seconds"),
    )


def get_counts_for_week(chat_id: int, user_ids: set[int], thread_ids: set[int], clean_date: date):
    if not user_ids or not thread_ids:
        return {}

    start, end = week_range_for_clean_date(clean_date)

    user_placeholders = ",".join("?" for _ in user_ids)
    thread_placeholders = ",".join("?" for _ in thread_ids)

    params = [
        chat_id,
        *list(user_ids),
        *list(thread_ids),
        start,
        end,
    ]

    query = f"""
        SELECT user_id, thread_id, COUNT(*) AS count
        FROM messages
        WHERE chat_id = ?
          AND user_id IN ({user_placeholders})
          AND thread_id IN ({thread_placeholders})
          AND created_at >= ?
          AND created_at < ?
        GROUP BY user_id, thread_id
    """

    counts = {}

    with db() as conn:
        rows = conn.execute(query, params).fetchall()

    for row in rows:
        counts[(int(row["user_id"]), int(row["thread_id"]))] = int(row["count"])

    return counts


def build_admin_ids():
    admins_sheet = get_worksheet("Список адмінів")
    rows = admins_sheet.get_all_values()

    admin_ids = set()

    for row in rows:
        if len(row) >= 3:
            value = str(row[2]).strip()
            if value.isdigit():
                admin_ids.add(int(value))

    return admin_ids


async def check_admin_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user

    if not user:
        await safe_send(update, context, "⛔ Не вдалося визначити користувача.")
        return False

    admin_ids = build_admin_ids()

    if user.id not in admin_ids:
        await safe_send(update, context, "⛔ У тебе немає доступу до цієї команди.")
        return False

    return True


def find_or_create_clean_date_column(registry_sheet, registry_values, clean_date: date) -> int:
    headers = registry_values[0]
    clean_date_text = clean_date.strftime("%d.%m.%Y")

    for i in range(2, len(headers), 2):
        if str(headers[i]).strip() == clean_date_text:
            return i + 1

    date_col = len(headers) + 1

    registry_sheet.update_cell(1, date_col, clean_date_text)
    registry_sheet.update_cell(1, date_col + 1, "Хто проводив")

    return date_col


def color_clean_registry(registry_sheet, members_sheet, rests_sheet):
    registry_values = registry_sheet.get_all_values()
    member_values = members_sheet.get_all_values()
    rest_values = rests_sheet.get_all_values()

    if not registry_values:
        return

    last_row = len(registry_values)
    last_col = len(registry_values[0])

    rest_map = {}
    join_date_map = {}

    for row in rest_values[1:]:
        if len(row) >= 8:
            character = str(row[1]).strip()
            start_date = parse_date(row[3])
            end_date = parse_date(row[4])
            status = str(row[7]).strip()

            if character and start_date and end_date and status == "Дійсний":
                rest_map[character] = {
                    "start": start_date,
                    "end": end_date,
                }

    for row in member_values[1:]:
        if len(row) >= 7:
            character = str(row[2]).strip()
            join_date = parse_date(row[6])

            if character and join_date:
                join_date_map[character] = join_date

    requests = []

    for row_index in range(2, last_row + 1):
        if len(registry_values[row_index - 1]) < 2:
            continue

        character = str(registry_values[row_index - 1][1]).strip()

        for col_index in range(3, last_col + 1, 2):
            date_header = parse_date(registry_values[0][col_index - 1])
            messages = ""

            if len(registry_values[row_index - 1]) >= col_index:
                messages = registry_values[row_index - 1][col_index - 1]

            color = None

            if date_header and messages != "":
                rest_info = rest_map.get(character)
                join_date = join_date_map.get(character)

                if join_date:
                    diff_days = (date_header - join_date).days

                    if 0 <= diff_days <= 7:
                        color = {"red": 0.85, "green": 0.85, "blue": 0.85}

                if color is None and rest_info:
                    if rest_info["start"] <= date_header <= rest_info["end"]:
                        color = {"red": 0.81, "green": 0.89, "blue": 0.95}

                if color is None and not rest_info:
                    try:
                        msg_count = int(messages)

                        if msg_count < 85:
                            color = {"red": 0.92, "green": 0.60, "blue": 0.60}
                        else:
                            color = {"red": 0.58, "green": 0.77, "blue": 0.49}
                    except ValueError:
                        color = None

            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": registry_sheet.id,
                        "startRowIndex": row_index - 1,
                        "endRowIndex": row_index,
                        "startColumnIndex": col_index - 1,
                        "endColumnIndex": col_index,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color if color else {
                                "red": 1,
                                "green": 1,
                                "blue": 1,
                            }
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

    if requests:
        registry_sheet.spreadsheet.batch_update({"requests": requests})


async def fill_clean_registry(update: Update, context: ContextTypes.DEFAULT_TYPE, clean_date: date):
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        await safe_send(update, context, "❌ Не вдалося визначити чат або користувача.")
        return

    cleaner = f"@{user.username}" if user.username else user.full_name

    registry_sheet = get_worksheet("Реєстр чистки")
    members_sheet = get_worksheet("Список учасників")
    branches_sheet = get_worksheet("Список гілок")
    rests_sheet = get_worksheet("Реєстр рестів")

    registry_values = registry_sheet.get_all_values()
    members_values = members_sheet.get_all_values()
    branches_values = branches_sheet.get_all_values()

    if not registry_values:
        await safe_send(update, context, "❌ Лист 'Реєстр чистки' порожній.")
        return

    branches = {}

    for row in branches_values[1:]:
        if len(row) >= 2 and row[0] and row[1]:
            try:
                branches[str(row[0]).strip()] = int(str(row[1]).strip())
            except ValueError:
                continue

    if not branches:
        await safe_send(update, context, "❌ Лист 'Список гілок' порожній або не містить номерів гілок.")
        return

    all_thread_ids = set(branches.values())

    members = {}

    for row in members_values[1:]:
        if len(row) < 4:
            continue

        fandom = str(row[1]).strip()
        character = str(row[2]).strip()
        user_id_raw = str(row[3]).strip()

        if not fandom or not character or not user_id_raw:
            continue

        if fandom == "N/A":
            continue

        if not user_id_raw.isdigit():
            continue

        key = (normalize_text(fandom), normalize_text(character))
        members[key] = int(user_id_raw)

    date_col = find_or_create_clean_date_column(registry_sheet, registry_values, clean_date)

    needed_user_ids = set()
    registry_rows_prepared = []

    for row_index, row in enumerate(registry_values[1:], start=2):
        code = str(row[0]).strip() if len(row) > 0 else ""
        character = str(row[1]).strip() if len(row) > 1 else ""

        if not code or not character or code == "N/A":
            continue

        member_key = (normalize_text(code), normalize_text(character))
        user_id = members.get(member_key)

        if not user_id:
            continue

        needed_user_ids.add(user_id)

        registry_rows_prepared.append({
            "row_index": row_index,
            "code": code,
            "character": character,
            "user_id": user_id,
        })

    counts = get_counts_for_week(
        chat_id=chat.id,
        user_ids=needed_user_ids,
        thread_ids=all_thread_ids,
        clean_date=clean_date,
    )

    updates = []
    filled = 0
    skipped = 0

    prepared_rows = {item["row_index"] for item in registry_rows_prepared}

    for row_index, row in enumerate(registry_values[1:], start=2):
        code = str(row[0]).strip() if len(row) > 0 else ""
        character = str(row[1]).strip() if len(row) > 1 else ""

        if code and character and row_index not in prepared_rows:
            skipped += 1

    for item in registry_rows_prepared:
        total = 0

        for thread_id in all_thread_ids:
            total += counts.get((item["user_id"], thread_id), 0)

        updates.append({
            "range": gspread.utils.rowcol_to_a1(item["row_index"], date_col),
            "values": [[total]],
        })

        updates.append({
            "range": gspread.utils.rowcol_to_a1(item["row_index"], date_col + 1),
            "values": [[cleaner]],
        })

        filled += 1

    if updates:
        registry_sheet.batch_update(updates)

    color_clean_registry(registry_sheet, members_sheet, rests_sheet)

    await safe_send(
        update,
        context,
        (
            f"✅ Чистку оновлено за {clean_date.strftime('%d.%m.%Y')}\n"
            f"Гілки враховано: {len(all_thread_ids)}\n"
            f"Заповнено: {filled}\n"
            f"Пропущено: {skipped}\n"
            f"Провів: {cleaner}"
        )
    )


async def clean_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    if not msg or not msg.text.startswith("/чистка"):
        return

    if not await check_admin_access(update, context):
        return

    today = datetime.now(LOCAL_TZ).date()

    if today.weekday() != 6:
        await safe_send(update, context, "⛔ Команда /чистка доступна тільки в неділю.")
        return

    await fill_clean_registry(update, context, current_sunday())


async def update_clean_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message

    if not msg:
        return

    if not await check_admin_access(update, context):
        return

    text = msg.text or ""
    parts = text.split()

    if len(parts) < 2:
        await safe_send(
            update,
            context,
            "❌ Вкажи дату: /оновити_чистку 07.06.2026"
        )
        return

    clean_date = parse_date(parts[1])

    if not clean_date:
        await safe_send(
            update,
            context,
            "❌ Неправильний формат дати. Приклад: /оновити_чистку 07.06.2026"
        )
        return

    await fill_clean_registry(update, context, clean_date)


async def thread_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(update, context, f"Thread ID: {get_thread_id(update)}")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не знайдено BOT_TOKEN. Додай змінну середовища BOT_TOKEN.")

    if not DB_PATH:
        raise RuntimeError("Не знайдено DB_PATH. Додай змінну середовища DB_PATH.")

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

    app.add_handler(CommandHandler("threadid", thread_id))

    app.add_handler(MessageHandler(filters.Regex(r"^/чистка($|\s)"), clean_stats))
    app.add_handler(MessageHandler(filters.Regex(r"^/оновити_чистку($|\s)"), update_clean_stats))
    app.add_handler(CommandHandler("update_clean", update_clean_stats))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_message))

    print("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
