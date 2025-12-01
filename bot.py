import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler



from typing import List, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from dateparser.search import search_dates

from config import TELEGRAM_BOT_TOKEN, TIMEZONE
from db import init_db, add_task, get_tasks, delete_task


LOCAL_TZ = ZoneInfo(TIMEZONE)

# Клавиатура внизу чата
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["Показать задачи", "Удалить задачу"]],
    resize_keyboard=True,
)


def parse_task_and_due(text: str) -> tuple[str, Optional[datetime]]:
    """
    Парсит текст задачи и дату/время, если они есть.

    Пример:
    "подготовиться к экзамену завтра в 18:00" ->
        ("подготовиться к экзамену", datetime)
    """
    raw = text.strip()

    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }

    matches = search_dates(raw, languages=["ru"], settings=settings)

    if not matches:
        return raw, None

    phrase, dt = matches[-1]

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)

    task_text = raw.replace(phrase, "").strip(" ,.-")

    if not task_text:
        task_text = raw

    return task_text, dt


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_first_name = update.effective_user.first_name
    text = (
        f"Привет, {user_first_name}!\n\n"
        "Я твой быстрый бот для задач.\n\n"
        "Просто напиши мне любую фразу, например:\n"
        "→ «Подготовиться к экзамену завтра в 18:00»\n\n"
        "Я сохраню задачу и поставлю напоминание.\n\n"
        "Кнопки внизу:\n"
        "• «Показать задачи» — увидеть список\n"
        "• «Удалить задачу» — удалить через кнопки"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Кнопки
    if text == "Показать задачи":
        await show_tasks(update, context)
        return

    if text == "Удалить задачу":
        await ask_delete_task(update, context)
        return

    # Игнор неизвестных команд
    if text.startswith("/"):
        await update.message.reply_text(
            "Неизвестная команда. Просто напиши текст задачи.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # Парсим текст и дату
    task_text, due_dt = parse_task_and_due(text)

    now = datetime.now(tz=LOCAL_TZ)
    if due_dt is not None and due_dt <= now:
        due_dt = None

    due_at_iso = due_dt.isoformat() if due_dt is not None else None

    # Сохраняем
    add_task(user_id=user_id, text=task_text, due_at_iso=due_at_iso)

    # Ставим напоминание, если есть дата
    if due_dt is not None and context.job_queue is not None:
        delta_seconds = (due_dt - now).total_seconds()
        context.job_queue.run_once(
            send_reminder,
            when=delta_seconds,
            chat_id=user_id,
            data={"task_text": task_text},
        )
        await update.message.reply_text(
            "Задача сохранена ✅\nНапоминание поставлено ⏰",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await update.message.reply_text(
            "Задача сохранена ✅",
            reply_markup=MAIN_KEYBOARD,
        )


async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    tasks: List[Tuple[int, str, Optional[str]]] = get_tasks(user_id)

    if not tasks:
        await update.message.reply_text(
            "У тебя пока нет задач 🙂\nПросто напиши мне что-нибудь, и я сохраню это как задачу.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    lines = []
    for idx, (task_id, text, due_at_iso) in enumerate(tasks, start=1):
        if due_at_iso:
            try:
                due_dt = datetime.fromisoformat(due_at_iso)
                due_local = due_dt.astimezone(LOCAL_TZ)
                due_str = due_local.strftime("%d.%m %H:%M")
                lines.append(f"{idx}. {text} (до {due_str})")
            except Exception:
                lines.append(f"{idx}. {text}")
        else:
            lines.append(f"{idx}. {text}")

    msg = "Твои задачи:\n\n" + "\n".join(lines)
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)


async def ask_delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    tasks: List[Tuple[int, str, Optional[str]]] = get_tasks(user_id)

    if not tasks:
        await update.message.reply_text(
            "Удалять пока нечего — список задач пуст 🙂",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    keyboard = []
    for task_id, text, _ in tasks:
        label = text if len(text) <= 25 else text[:22] + "..."
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"❌ {label}", callback_data=f"del:{task_id}"
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выбери задачу, которую хочешь удалить:",
        reply_markup=reply_markup,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    if not data.startswith("del:"):
        return

    try:
        task_id = int(data.split(":", maxsplit=1)[1])
    except ValueError:
        return

    user_id = query.from_user.id
    delete_task(user_id=user_id, task_id=task_id)

    tasks = get_tasks(user_id)
    if not tasks:
        text = "Задача удалена ✅\n\nСписок задач теперь пуст."
    else:
        lines = []
        for idx, (tid, ttext, due_at_iso) in enumerate(tasks, start=1):
            if due_at_iso:
                try:
                    due_dt = datetime.fromisoformat(due_at_iso)
                    due_local = due_dt.astimezone(LOCAL_TZ)
                    due_str = due_local.strftime("%d.%m %H:%M")
                    lines.append(f"{idx}. {ttext} (до {due_str})")
                except Exception:
                    lines.append(f"{idx}. {ttext}")
            else:
                lines.append(f"{idx}. {ttext}")
        text = "Задача удалена ✅\n\nАктуальный список задач:\n\n" + "\n".join(lines)

    await query.edit_message_text(text=text)


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    if not job:
        return

    data = job.data or {}
    task_text = data.get("task_text", "задача")
    chat_id = job.chat_id

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Напоминание:\n\n{task_text}",
    )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_health_server():
    """Простой HTTP-сервер для Render, чтобы был открыт порт."""
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def main():
    init_db()

    # Запускаем HTTP-сервер для Render в отдельном потоке
    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    print("Бот запущен. Нажми Ctrl+C, чтобы остановить.")
    app.run_polling()
