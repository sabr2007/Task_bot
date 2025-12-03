from typing import List, Tuple, Optional
from datetime import datetime, time as dtime, timedelta
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
from db import (
    init_db,
    add_task,
    get_tasks,
    delete_task,
    get_archived_tasks,
    set_task_done,
    get_users_with_tasks,
    get_task,
    update_task_due,
)

LOCAL_TZ = ZoneInfo(TIMEZONE)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Показать задачи", "Удалить задачу"],
        ["Отметить выполненной", "Архив задач"],
    ],
    resize_keyboard=True,
)

def format_tasks_message(
    title: str,
    tasks: List[Tuple[int, str, Optional[str]]],
) -> str:
    """Формирует текст со списком задач, разделённых на с дедлайном и без."""
    if not tasks:
        return f"{title}:\n\n(пока пусто)"

    with_deadline: List[str] = []
    without_deadline: List[str] = []

    for _task_id, text, due_at_iso in tasks:
        if due_at_iso:
            try:
                due_dt = datetime.fromisoformat(due_at_iso)
                due_local = due_dt.astimezone(LOCAL_TZ)
                due_str = due_local.strftime("%d.%m %H:%M")
                item = f"{text} (до {due_str})"
            except Exception:
                item = text
            with_deadline.append(item)
        else:
            without_deadline.append(text)

    parts: List[str] = [f"{title}:\n"]

    if with_deadline:
        lines = [f"{i}. {t}" for i, t in enumerate(with_deadline, start=1)]
        parts.append("🕒 Задачи с дедлайном:\n" + "\n".join(lines) + "\n")

    if without_deadline:
        lines = [f"{i}. {t}" for i, t in enumerate(without_deadline, start=1)]
        parts.append("📝 Без дедлайна:\n" + "\n".join(lines))

    return "\n".join(parts).strip()

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

    if text == "Показать задачи":
        await show_tasks(update, context)
        return

    if text == "Удалить задачу":
        await ask_delete_task(update, context)
        return

    if text == "Архив задач":
        await show_archive(update, context)
        return

    if text == "Отметить выполненной":
        await ask_done_task(update, context)
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
    task_id = add_task(user_id=user_id, text=task_text, due_at_iso=due_at_iso)

    # Ставим напоминание, если есть дата
    if due_dt is not None and context.job_queue is not None:
        delta_seconds = (due_dt - now).total_seconds()
        context.job_queue.run_once(
            send_reminder,
            when=delta_seconds,
            chat_id=user_id,
            data={"task_id": task_id, "task_text": task_text},
        )

        # Предложение выбрать, за сколько времени напомнить
        keyboard = [
            [
                InlineKeyboardButton("За 5 минут", callback_data=f"set_remind:{task_id}:5"),
                InlineKeyboardButton("За 10 минут", callback_data=f"set_remind:{task_id}:10"),
            ],
            [
                InlineKeyboardButton("За 1 час", callback_data=f"set_remind:{task_id}:60"),
            ],
            [
                InlineKeyboardButton("Только в момент дедлайна", callback_data=f"set_remind:{task_id}:exact"),
            ],
        ]

        await update.message.reply_text(
            "Задача сохранена ✅\nВыберите, за сколько времени напомнить:",
            reply_markup=InlineKeyboardMarkup(keyboard),
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

    msg = format_tasks_message("Твои задачи", tasks)
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

async def show_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список выполненных задач пользователя."""
    if not update.message:
        return

    user_id = update.effective_user.id
    tasks: List[Tuple[int, str, Optional[str]]] = get_archived_tasks(user_id)

    if not tasks:
        await update.message.reply_text(
            "Архив задач пуст 🙂",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    msg = format_tasks_message("Архив выполненных задач", tasks)
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

async def ask_done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает задачи с inline-кнопками для отметки 'выполнено'."""
    if not update.message:
        return

    user_id = update.effective_user.id
    tasks: List[Tuple[int, str, Optional[str]]] = get_tasks(user_id)

    if not tasks:
        await update.message.reply_text(
            "Пока нет активных задач, которые можно отметить выполненными 🙂",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    keyboard = []
    for task_id, text, _ in tasks:
        label = text if len(text) <= 25 else text[:22] + "..."
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"✅ {label}", callback_data=f"done:{task_id}"
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Выбери задачу, которую хочешь отметить выполненной:",
        reply_markup=reply_markup,
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data or ""

    # Отметка задачи выполненной из напоминания
    if data.startswith("rem_done:"):
        try:
            task_id = int(data.split(":", maxsplit=1)[1])
        except ValueError:
            return

        user_id = query.from_user.id
        set_task_done(user_id=user_id, task_id=task_id)

        await query.edit_message_text("Задача отмечена выполненной ✅")
        return

    # Показать варианты отсрочки
    # Показать варианты отсрочки
    if data.startswith("rem_snooze_menu:"):
        try:
            task_id = int(data.split(":", maxsplit=1)[1])
        except ValueError:
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    "На 5 минут",
                    callback_data=f"rem_snooze:{task_id}:5",
                )
            ],
            [
                InlineKeyboardButton(
                    "На 10 минут",
                    callback_data=f"rem_snooze:{task_id}:10",
                )
            ],
            [
                InlineKeyboardButton(
                    "На 1 час",
                    callback_data=f"rem_snooze:{task_id}:60",
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ Назад",
                    callback_data=f"rem_back:{task_id}",
                )
            ],
        ]

        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return


    # Отложить напоминание
    if data.startswith("rem_snooze:"):
        parts = data.split(":")
        if len(parts) != 3:
            return

        try:
            task_id = int(parts[1])
            minutes = int(parts[2])
        except ValueError:
            return

        user_id = query.from_user.id

        row = get_task(user_id=user_id, task_id=task_id)
        if not row:
            await query.edit_message_text(
                "Задача не найдена. Возможно, она была удалена."
            )
            return

        _tid, task_text, _due_at = row

        now = datetime.now(tz=LOCAL_TZ)
        new_due = now + timedelta(minutes=minutes)
        new_due_iso = new_due.isoformat()

        # обновляем дедлайн в базе
        update_task_due(user_id=user_id, task_id=task_id, due_at_iso=new_due_iso)

        # ставим новое напоминание
        if context.job_queue is not None:
            delay = (new_due - now).total_seconds()
            context.job_queue.run_once(
                send_reminder,
                when=delay,
                chat_id=user_id,
                data={"task_id": task_id, "task_text": task_text},
            )

        # красивый текст с указанием времени следующего напоминания
        next_time_str = new_due.strftime("%H:%M")
        await query.edit_message_text(
            f"Напоминание отложено на {minutes} минут ⏰\n"
            f"Следующее напоминание: {next_time_str}"
        )
        return

    # Вернуться к исходным кнопкам "Выполнено / Отложить"
    if data.startswith("rem_back:"):
        try:
            task_id = int(data.split(":", maxsplit=1)[1])
        except ValueError:
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    "Выполнено ✅", callback_data=f"rem_done:{task_id}"
                ),
                InlineKeyboardButton(
                    "Отложить ⏰", callback_data=f"rem_snooze_menu:{task_id}"
                ),
            ]
        ]

        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return


    # Установка времени напоминания
    if data.startswith("set_remind:"):
        _, task_id_str, mode = data.split(":")

        try:
            task_id = int(task_id_str)
        except ValueError:
            return

        row = get_task(query.from_user.id, task_id)
        if not row:
            await query.edit_message_text("Задача не найдена.")
            return

        _tid, task_text, due_iso = row
        if not due_iso:
            await query.edit_message_text("У задачи нет дедлайна — напоминание невозможно.")
            return

        due_dt = datetime.fromisoformat(due_iso).astimezone(LOCAL_TZ)
        now = datetime.now(tz=LOCAL_TZ)

        # Режим: напомнить В МОМЕНТ дедлайна
        if mode == "exact":
            delay = (due_dt - now).total_seconds()
            context.job_queue.run_once(
                send_reminder,
                when=delay,
                chat_id=query.from_user.id,
                data={"task_id": task_id, "task_text": task_text},
            )

            await query.edit_message_text(
                f"Напоминание придёт в момент дедлайна: {due_dt.strftime('%H:%M')} ⏰"
            )
            return

        # Режим: заранее
        minutes = int(mode)
        remind_time = due_dt - timedelta(minutes=minutes)

        # если напоминание уже "прошло" → переносим на ближайшие 5 сек
        if remind_time <= now:
            remind_time = now + timedelta(seconds=5)

        delay = (remind_time - now).total_seconds()

        context.job_queue.run_once(
            send_reminder,
            when=delay,
            chat_id=query.from_user.id,
            data={"task_id": task_id, "task_text": task_text},
        )

        await query.edit_message_text(
            f"Напоминание будет отправлено в {remind_time.strftime('%H:%M')} ⏰"
        )
        return

    # Удаление задачи
    if data.startswith("del:"):
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
        return

    # Отметка задачи выполненной
    if data.startswith("done:"):
        try:
            task_id = int(data.split(":", maxsplit=1)[1])
        except ValueError:
            return

        user_id = query.from_user.id
        set_task_done(user_id=user_id, task_id=task_id)

        tasks = get_tasks(user_id)
        if not tasks:
            text = "Задача отмечена выполненной ✅\n\nАктивных задач больше нет."
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
            text = (
                "Задача отмечена выполненной ✅\n\n"
                "Актуальный список активных задач:\n\n" + "\n".join(lines)
            )

        await query.edit_message_text(text=text)
        return

async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Утренний дайджест задач всем пользователям с активными задачами."""
    user_ids = get_users_with_tasks()

    if not user_ids:
        return

    for user_id in user_ids:
        tasks = get_tasks(user_id)
        if not tasks:
            continue

        msg = format_tasks_message(
            "Утренний дайджест задач на сегодня",
            tasks,
        )

        await context.bot.send_message(
            chat_id=user_id,
            text=msg,
            reply_markup=MAIN_KEYBOARD,
        )

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    if not job:
        return

    data = job.data or {}
    task_text = data.get("task_text", "задача")
    task_id = data.get("task_id")
    chat_id = job.chat_id

    # Если по какой-то причине id не передали — просто текстом
    if task_id is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Напоминание:\n\n{task_text}",
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "Выполнено ✅", callback_data=f"rem_done:{task_id}"
            ),
            InlineKeyboardButton(
                "Отложить ⏰", callback_data=f"rem_snooze_menu:{task_id}"
            ),
        ]
    ]

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Напоминание:\n\n{task_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

def main():
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    # Ежедневный дайджест в 07:30 по локальному времени
    if app.job_queue is not None:
        app.job_queue.run_daily(
            send_daily_digest,
            time=dtime(hour=7, minute=30, tzinfo=LOCAL_TZ),
            name="daily_digest",
        )

    print("Бот запущен. Нажми Ctrl+C, чтобы остановить.")
    app.run_polling()

if __name__ == "__main__":
    main()
