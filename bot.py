import logging
import asyncio
import sqlite3
import re
from typing import List, Tuple, Optional
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
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

# Импорты из ваших файлов
from config import TELEGRAM_BOT_TOKEN, TIMEZONE, DB_PATH
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
    update_task_text,
    log_event,
)

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Константы ---
LOCAL_TZ = ZoneInfo(TIMEZONE)
ADMIN_USER_ID = 6113692933

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Показать задачи", "Удалить задачу"],
        ["Отметить выполненной", "Еще"],
    ],
    resize_keyboard=True,
)

EXTRA_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Что бот умеет", "Архив задач"],
        ["Назад"],
    ],
    resize_keyboard=True,
)


# ==========================================
# Вспомогательные функции (Логика и Форматирование)
# ==========================================

async def restore_reminders_on_startup(app):
    """
    Восстанавливает таймеры напоминаний из базы данных при запуске бота.
    """
    logger.info("Восстановление напоминаний...")
    try:
        # Прямой запрос к БД, чтобы не менять db.py, но получить ВСЕ задачи всех пользователей
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, user_id, text, due_at 
                FROM tasks 
                WHERE (status IS NULL OR status = 'active') 
                  AND due_at IS NOT NULL
                """
            )
            tasks = cursor.fetchall()

        restored_count = 0
        now = datetime.now(tz=LOCAL_TZ)

        for task_id, user_id, text, due_at_iso in tasks:
            try:
                due_dt = datetime.fromisoformat(due_at_iso).astimezone(LOCAL_TZ)
                
                # Если дедлайн в будущем - ставим таймер
                if due_dt > now:
                    delta = (due_dt - now).total_seconds()
                    app.job_queue.run_once(
                        send_reminder,
                        when=delta,
                        chat_id=user_id,
                        data={"task_id": task_id, "task_text": text},
                    )
                    restored_count += 1
            except Exception as e:
                logger.error(f"Ошибка восстановления задачи {task_id}: {e}")

        logger.info(f"Восстановлено напоминаний: {restored_count}")

    except Exception as e:
        logger.error(f"Критическая ошибка при восстановлении напоминаний: {e}")


def format_tasks_message(title: str, tasks: List[Tuple[int, str, Optional[str]]]) -> str:
    """Формирует текст со списком задач."""
    if not tasks:
        return f"{title}:\n\n(пока пусто)"

    # Сортировка: сначала с дедлайнами (по возрастанию), потом без дедлайнов
    def sort_key(item):
        _id, _text, _due = item
        if _due:
            return (0, _due) # Приоритет 0, сортировка по дате
        return (1, 0)        # Приоритет 1 (в конец)

    sorted_tasks = sorted(tasks, key=sort_key)

    with_deadline = []
    without_deadline = []

    for _task_id, text, due_at_iso in sorted_tasks:
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

    parts = [f"{title}:\n"]

    if with_deadline:
        lines = [f"{i}. {t}" for i, t in enumerate(with_deadline, start=1)]
        parts.append("🕒 Задачи с дедлайном:\n" + "\n".join(lines) + "\n")

    if without_deadline:
        # Нумерацию продолжаем или начинаем заново? Обычно лучше заново для блока
        lines = [f"{i}. {t}" for i, t in enumerate(without_deadline, start=1)]
        parts.append("📝 Без дедлайна:\n" + "\n".join(lines))

    return "\n".join(parts).strip()


def normalize_russian_time_phrases(raw: str) -> str:
    """
    Нормализует фразы вида 'в 2 часа дня' -> '14:00',
    чтобы dateparser лучше их понимал, если регулярки не справятся.
    """
    text = raw
    if "через" in text.lower():
        return text

    pattern = re.compile(
        r"\b(\d{1,2})\s*час(?:а|ов)?\s*(утра|дня|вечера|ночи)\b",
        re.IGNORECASE,
    )

    def repl(match: re.Match) -> str:
        hour = int(match.group(1))
        part_of_day = match.group(2).lower()

        if part_of_day == "утра":
            if hour == 12: hour = 0
        elif part_of_day in ("дня", "вечера"):
            if 1 <= hour <= 11: hour += 12
        elif part_of_day == "ночи":
            if hour == 12: hour = 0
            
        return f"{hour:02d}:00"

    return re.sub(pattern, repl, text)


def parse_task_and_due(text: str) -> tuple[str, Optional[datetime]]:
    """
    Гибридный парсер:
    1. Сначала ищет явное время (Regex).
    2. Потом ищет дату (Dateparser).
    3. Объединяет результаты.
    """
    raw = text.strip()
    now = datetime.now(tz=LOCAL_TZ)

    # Хранилище для найденного времени
    found_time: Optional[dtime] = None
    
    # Текст, из которого мы будем вырезать найденные куски времени
    clean_text_for_date = raw

    # --- ШАГ 1: Ищем ВРЕМЯ регулярками ---

    # 1.1 Шаблон "до/к 4", "до/к 16:30"
    # Группы: 1="до/к", 2="часы", 3="минуты"
    m_due = re.search(r"\b(до|к)\s+(\d{1,2})(?::(\d{2}))?\b", raw, flags=re.IGNORECASE)
    if m_due:
        hour = int(m_due.group(2))
        minute = int(m_due.group(3) or 0)
        
        # Эвристика: "до 4" -> 16:00, если не уточнено иначе
        if 1 <= hour <= 8:
            hour += 12
            
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            found_time = dtime(hour, minute)
            clean_text_for_date = raw.replace(m_due.group(0), " ")

    # 1.2 Если первый шаблон не сработал, пробуем "в 7 вечера", "в 18:00"
    # Группы: 1="часы", 2="минуты", 3="утра/дня/..."
    if not found_time:
        m_at = re.search(
            r"\b(?:в|на)\s+(\d{1,2})(?::(\d{2}))?\s*(?:часа|часов|час|ч)?\s*(утра|дня|вечера|ночи)?\b",
            raw, flags=re.IGNORECASE
        )
        if m_at:
            hour = int(m_at.group(1))
            minute = int(m_at.group(2) or 0)
            # ВОТ ТУТ БЫЛА ОШИБКА: берем группу 3, а не 4
            mer = (m_at.group(3) or "").lower() 

            if mer in ("дня", "вечера") and 1 <= hour <= 11:
                hour += 12
            elif mer == "утра" and hour == 12:
                hour = 0
            elif mer == "ночи" and hour == 12:
                hour = 0
            
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                found_time = dtime(hour, minute)
                clean_text_for_date = raw.replace(m_at.group(0), " ")

    # --- ШАГ 2: Ищем ДАТУ через dateparser ---
    
    normalized_text = normalize_russian_time_phrases(clean_text_for_date)
    
    settings = {
        "TIMEZONE": TIMEZONE,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
    }
    
    matches = search_dates(normalized_text, languages=["ru"], settings=settings)
    
    # --- ШАГ 3: Сборка результата ---

    final_dt: Optional[datetime] = None
    extracted_phrase = ""

    if matches:
        # dateparser нашел дату (например, "завтра" или "суббота")
        found_phrase, parse_dt = matches[-1]
        
        if found_time:
            # Склеиваем дату от dateparser и время от regex
            final_dt = parse_dt.replace(hour=found_time.hour, minute=found_time.minute, second=0)
            extracted_phrase = found_phrase
        else:
            # Только dateparser
            final_dt = parse_dt
            extracted_phrase = found_phrase
            
    else:
        # dateparser не нашел дату, но есть время от regex
        if found_time:
            candidate = now.replace(
                hour=found_time.hour, minute=found_time.minute, second=0, microsecond=0
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            final_dt = candidate

    # Формируем чистый текст задачи
    if final_dt:
        # Удаляем куски времени/даты из текста
        raw_clean = raw
        if found_time and m_due:
            raw_clean = raw_clean.replace(m_due.group(0), "")
        elif found_time and m_at:
            raw_clean = raw_clean.replace(m_at.group(0), "")
        
        if extracted_phrase:
            raw_clean = raw_clean.replace(extracted_phrase, "")
            
        task_text = raw_clean.strip(" ,.-") 
        if not task_text: 
            task_text = "Задача"
        return task_text, final_dt

    return raw, None


# ==========================================
# Обработчики команд и текста
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_first_name = update.effective_user.first_name
    log_event(user_id=update.effective_user.id, event_type="start")

    text = (
        f"Здравствуйте, {user_first_name}!\n\n"
        "Это ваш личный бот-органайзер задач. Сейчас он в режиме бета-тестирования.\n\n"
        "Отправьте мне любую задачу текстом, например:\n"
        "«Подготовиться к экзамену завтра в 18:00» — я сохраню её и помогу с напоминанием.\n\n"
        "Основные кнопки внизу.\n"
        "Чтобы посмотреть, что бот умеет, нажмите: «Еще» → «Что бот умеет»."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    # --- Режим редактирования задачи ---
    edit_task_id = context.user_data.get("edit_task_id")
    if edit_task_id is not None:
        await process_edit_task_text(update, context, user_id, text, edit_task_id)
        return

    # --- Меню и Команды ---
    if text == "Еще":
        await update.message.reply_text("Дополнительные функции:", reply_markup=EXTRA_KEYBOARD)
        return
    if text == "Назад":
        await update.message.reply_text("Возвращаюсь к основному меню.", reply_markup=MAIN_KEYBOARD)
        return
    if text == "Что бот умеет":
        await show_help(update, context)
        return
    if text == "Показать задачи":
        log_event(user_id, "tasks_shown")
        await show_tasks(update, context)
        return
    if text == "Удалить задачу":
        log_event(user_id, "delete_menu_opened")
        await ask_delete_task(update, context)
        return
    if text == "Архив задач":
        log_event(user_id, "archive_opened")
        await show_archive(update, context)
        return
    if text == "Отметить выполненной":
        log_event(user_id, "mark_done_menu_opened")
        await ask_done_task(update, context)
        return
    if text.startswith("/"):
        await update.message.reply_text(
            "Неизвестная команда. Просто напиши текст задачи.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    # --- Создание новой задачи ---
    await create_new_task(update, context, user_id, text)


async def process_edit_task_text(update, context, user_id, text, edit_task_id):
    row = get_task(user_id, edit_task_id)
    if not row:
        await update.message.reply_text("Задача не найдена.", reply_markup=MAIN_KEYBOARD)
        context.user_data.pop("edit_task_id", None)
        return

    _tid, old_text, old_due_iso = row
    new_text, new_due_dt = parse_task_and_due(text)
    if not new_text:
        new_text = old_text

    now = datetime.now(tz=LOCAL_TZ)
    if new_due_dt is None:
        new_due_iso = old_due_iso
    else:
        new_due_iso = None if new_due_dt <= now else new_due_dt.isoformat()

    update_task_text(user_id, edit_task_id, new_text)
    update_task_due(user_id, edit_task_id, new_due_iso)

    log_event(user_id=user_id, event_type="task_edited", task_id=edit_task_id)
    context.user_data.pop("edit_task_id", None)

    await update.message.reply_text("Задача обновлена ✏️", reply_markup=MAIN_KEYBOARD)


async def create_new_task(update, context, user_id, text):
    task_text, due_dt = parse_task_and_due(text)
    now = datetime.now(tz=LOCAL_TZ)

    if due_dt is not None and due_dt <= now:
        due_dt = None

    due_at_iso = due_dt.isoformat() if due_dt else None
    task_id = add_task(user_id=user_id, text=task_text, due_at_iso=due_at_iso)

    log_event(user_id=user_id, event_type="task_created", task_id=task_id)

    if due_dt is not None and context.job_queue is not None:
        delta_seconds = (due_dt - now).total_seconds()
        # Ставим таймер напоминания
        context.job_queue.run_once(
            send_reminder,
            when=delta_seconds,
            chat_id=user_id,
            data={"task_id": task_id, "task_text": task_text},
        )

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
            "Не обнаружил дату или время.\nЗаписал задачу как *без дедлайна* ✅",
            reply_markup=MAIN_KEYBOARD,
            parse_mode="Markdown",
        )


async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    if not tasks:
        await update.message.reply_text(
            "У тебя пока нет задач 🙂\nПросто напиши мне что-нибудь.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    msg = format_tasks_message("Твои задачи", tasks)
    keyboard = [[InlineKeyboardButton("✏️ Редактировать задачу", callback_data="edit_list")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🧠 Что умеет этот бот:\n\n"
        "• Сохранять задачи из обычного текста.\n"
        "• Понимать даты и время на русском.\n"
        "• Ставить напоминания к дедлайнам.\n"
        "• Показывать список активных задач.\n"
        "• Отмечать задачи выполненными и хранить архив.\n"
        "• Откладывать напоминания (Snooze).\n"
        "• Редактировать текст задачи и её дедлайн.\n\n"
        "Если что-то работает странно — пишите @sabrval😊"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=EXTRA_KEYBOARD)


async def show_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.effective_user.id
    tasks = get_archived_tasks(user_id)
    if not tasks:
        await update.message.reply_text("Архив задач пуст 🙂", reply_markup=MAIN_KEYBOARD)
        return

    lines = []
    for idx, (_task_id, text, _due, completed_at_iso) in enumerate(tasks, start=1):
        parts = [f"{idx}. ✅ {text}"]
        if completed_at_iso:
            try:
                completed_dt = datetime.fromisoformat(completed_at_iso).astimezone(LOCAL_TZ)
                parts.append(f"выполнено {completed_dt.strftime('%d.%m %H:%M')}")
            except Exception:
                pass
        lines.append(" — ".join(parts))

    msg = "Архив выполненных задач:\n\n" + "\n".join(lines)
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)


async def ask_delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    if not tasks:
        await update.message.reply_text("Удалять пока нечего.", reply_markup=MAIN_KEYBOARD)
        return

    keyboard = []
    for task_id, text, _ in tasks:
        label = text[:22] + "..." if len(text) > 25 else text
        keyboard.append([InlineKeyboardButton(f"❌ {label}", callback_data=f"del:{task_id}")])

    await update.message.reply_text(
        "Выбери задачу, которую хочешь удалить:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def ask_done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    if not tasks:
        await update.message.reply_text("Нет активных задач для выполнения.", reply_markup=MAIN_KEYBOARD)
        return

    keyboard = []
    for task_id, text, _ in tasks:
        label = text[:22] + "..." if len(text) > 25 else text
        keyboard.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"done:{task_id}")])

    await update.message.reply_text(
        "Выбери задачу, которую хочешь отметить выполненной:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ==========================================
# Callback Handlers 
# ==========================================

async def on_reminder_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: rem_done:ID"""
    query = update.callback_query
    await query.answer()
    try:
        task_id = int(query.data.split(":", maxsplit=1)[1])
    except ValueError:
        return

    user_id = query.from_user.id
    set_task_done(user_id, task_id)
    log_event(user_id, "task_done_from_reminder", task_id)
    await query.edit_message_text("Задача отмечена выполненной ✅")


async def on_reminder_snooze_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: rem_snooze_menu:ID"""
    query = update.callback_query
    await query.answer()
    try:
        task_id = int(query.data.split(":", maxsplit=1)[1])
    except ValueError:
        return

    keyboard = [
        [InlineKeyboardButton("На 5 минут", callback_data=f"rem_snooze:{task_id}:5")],
        [InlineKeyboardButton("На 10 минут", callback_data=f"rem_snooze:{task_id}:10")],
        [InlineKeyboardButton("На 1 час", callback_data=f"rem_snooze:{task_id}:60")],
        [InlineKeyboardButton("↩️ Назад", callback_data=f"rem_back:{task_id}")],
    ]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


async def on_reminder_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: rem_snooze:ID:MINUTES"""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) != 3: return

    try:
        task_id, minutes = int(parts[1]), int(parts[2])
    except ValueError:
        return

    user_id = query.from_user.id
    row = get_task(user_id, task_id)
    if not row:
        await query.edit_message_text("Задача не найдена или удалена.")
        return

    _tid, task_text, _ = row
    now = datetime.now(tz=LOCAL_TZ)
    new_due = now + timedelta(minutes=minutes)
    new_due_iso = new_due.isoformat()

    update_task_due(user_id, task_id, new_due_iso)

    if context.job_queue:
        delay = (new_due - now).total_seconds()
        context.job_queue.run_once(
            send_reminder,
            when=delay,
            chat_id=user_id,
            data={"task_id": task_id, "task_text": task_text},
        )
    
    log_event(user_id, "reminder_snoozed", task_id, meta={"minutes": minutes})
    await query.edit_message_text(
        f"Напоминание отложено на {minutes} минут ⏰\nСледующее: {new_due.strftime('%H:%M')}"
    )


async def on_reminder_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: rem_back:ID"""
    query = update.callback_query
    await query.answer()
    try:
        task_id = int(query.data.split(":", maxsplit=1)[1])
    except ValueError: return

    keyboard = [
        [
            InlineKeyboardButton("Выполнено ✅", callback_data=f"rem_done:{task_id}"),
            InlineKeyboardButton("Отложить ⏰", callback_data=f"rem_snooze_menu:{task_id}"),
        ]
    ]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


async def on_set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: set_remind:ID:MODE"""
    query = update.callback_query
    await query.answer()
    try:
        _, task_id_str, mode = query.data.split(":")
        task_id = int(task_id_str)
    except ValueError: return

    user_id = query.from_user.id
    row = get_task(user_id, task_id)
    if not row:
        await query.edit_message_text("Задача не найдена.")
        return

    _tid, task_text, due_iso = row
    if not due_iso:
        await query.edit_message_text("У задачи нет дедлайна.")
        return

    due_dt = datetime.fromisoformat(due_iso).astimezone(LOCAL_TZ)
    now = datetime.now(tz=LOCAL_TZ)
    
    # Режим
    if mode == "exact":
        delay = (due_dt - now).total_seconds()
        remind_time_str = due_dt.strftime('%H:%M')
    else:
        minutes = int(mode)
        remind_time = due_dt - timedelta(minutes=minutes)
        if remind_time <= now: remind_time = now + timedelta(seconds=5)
        delay = (remind_time - now).total_seconds()
        remind_time_str = remind_time.strftime('%H:%M')

    if context.job_queue:
        context.job_queue.run_once(
            send_reminder,
            when=delay,
            chat_id=user_id,
            data={"task_id": task_id, "task_text": task_text},
        )

    log_event(user_id, "remind_option_chosen", task_id, meta={"mode": mode})
    await query.edit_message_text(f"Напоминание установлено на {remind_time_str} ⏰")


async def on_edit_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: edit_list"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    log_event(user_id, "edit_list_opened")
    tasks = get_tasks(user_id)

    if not tasks:
        await query.edit_message_text("Нет задач для редактирования 🙂")
        return

    keyboard = []
    for task_id, text, _ in tasks:
        label = text[:22] + "..." if len(text) > 25 else text
        keyboard.append([InlineKeyboardButton(f"✏️ {label}", callback_data=f"edit:{task_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_back_to_tasks")])

    await query.edit_message_text("Выбери задачу для редактирования:", reply_markup=InlineKeyboardMarkup(keyboard))


async def on_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: edit:ID"""
    query = update.callback_query
    await query.answer()
    try:
        task_id = int(query.data.split(":")[1])
    except ValueError: return

    context.user_data["edit_task_id"] = task_id
    log_event(query.from_user.id, "task_edit_started", task_id)
    await query.edit_message_text(
        "✏️ Введите новый текст задачи.\n❗ Не забудьте указать новый дедлайн.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="edit_back_to_tasks")]]),
    )


async def on_edit_back_to_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: edit_back_to_tasks"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    context.user_data.pop("edit_task_id", None)
    tasks = get_tasks(user_id)
    if not tasks:
        await query.edit_message_text("Задач нет.")
        return
    msg = format_tasks_message("Твои задачи", tasks)
    keyboard = [[InlineKeyboardButton("✏️ Редактировать задачу", callback_data="edit_list")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))


async def on_delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: del:ID"""
    query = update.callback_query
    await query.answer()
    try:
        task_id = int(query.data.split(":")[1])
    except ValueError: return

    user_id = query.from_user.id
    delete_task(user_id, task_id)

    tasks = get_tasks(user_id)
    if not tasks:
        text = "Задача удалена ✅\n\nСписок задач теперь пуст."
    else:
        text = "Задача удалена ✅\n\n" + format_tasks_message("Актуальный список задач", tasks)
    
    await query.edit_message_text(text)


async def on_done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback: done:ID"""
    query = update.callback_query
    await query.answer()
    try:
        task_id = int(query.data.split(":")[1])
    except ValueError: return

    user_id = query.from_user.id
    set_task_done(user_id, task_id)
    log_event(user_id, "task_marked_done", task_id)

    tasks = get_tasks(user_id)
    if not tasks:
        text = "Задача отмечена выполненной ✅\n\nАктивных задач больше нет."
    else:
        text = "Задача отмечена выполненной ✅\n\n" + format_tasks_message("Актуальный список задач", tasks)
    
    await query.edit_message_text(text)


# ==========================================
# Фоновые задачи (Jobs) и Админка
# ==========================================

async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    user_ids = get_users_with_tasks()
    if not user_ids: return

    for user_id in user_ids:
        tasks = get_tasks(user_id)
        if not tasks: continue
        msg = format_tasks_message("Утренний дайджест задач на сегодня", tasks)
        try:
            await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            logger.warning(f"Не удалось отправить дайджест юзеру {user_id}: {e}")


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    if not job: return
    data = job.data or {}
    task_text = data.get("task_text", "задача")
    task_id = data.get("task_id")
    chat_id = job.chat_id

    # Клавиатура "Выполнено / Отложить" прямо в напоминании
    keyboard = []
    if task_id:
        keyboard.append([
            InlineKeyboardButton("Выполнено ✅", callback_data=f"rem_done:{task_id}"),
            InlineKeyboardButton("Отложить ⏰", callback_data=f"rem_snooze_menu:{task_id}"),
        ])

    log_event(user_id=chat_id, event_type="reminder_sent", task_id=task_id)
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Напоминание:\n\n{task_text}",
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить напоминание {chat_id}: {e}")


async def dump_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("Недостаточно прав.")
        return
    try:
        with open(DB_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_USER_ID,
                document=InputFile(f, filename="tasks.db"),
                caption="Снимок базы задач",
            )
    except FileNotFoundError:
        await update.message.reply_text("Файл базы не найден.")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("Только админ может делать рассылку.")
        return

    args_text = " ".join(context.args) if context.args else ""
    reply_msg = update.message.reply_to_message
    if args_text:
        broadcast_text = args_text
    elif reply_msg and reply_msg.text:
        broadcast_text = reply_msg.text
    else:
        await update.message.reply_text("Использование: /broadcast Текст")
        return

    recipients = get_users_with_tasks()
    sent = 0
    failed = 0

    status_msg = await update.message.reply_text(f"Начинаю рассылку на {len(recipients)} чел...")

    for uid in recipients:
        try:
            await context.bot.send_message(chat_id=uid, text=broadcast_text)
            sent += 1
            # Пауза, чтобы не словить FloodWait от Telegram
            await asyncio.sleep(0.05) 
        except Exception:
            failed += 1

    log_event(user_id, "broadcast_sent", meta={"sent": sent, "failed": failed})
    await context.bot.edit_message_text(
        chat_id=user_id,
        message_id=status_msg.message_id,
        text=f"Рассылка завершена.\n✅ Успешно: {sent}\n❌ Ошибок: {failed}"
    )


# ==========================================
# MAIN
# ==========================================

def main():
    # Инициализация БД
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # --- Восстановление напоминаний при старте ---
    # Мы используем post_init, чтобы получить доступ к job_queue после инициализации app
    app.post_init = restore_reminders_on_startup

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("dumpdb", dump_db))
    app.add_handler(CommandHandler("broadcast", broadcast))

    # Callback Handlers (Используем pattern для маршрутизации)
    app.add_handler(CallbackQueryHandler(on_reminder_done, pattern=r"^rem_done:"))
    app.add_handler(CallbackQueryHandler(on_reminder_snooze_menu, pattern=r"^rem_snooze_menu:"))
    app.add_handler(CallbackQueryHandler(on_reminder_snooze, pattern=r"^rem_snooze:"))
    app.add_handler(CallbackQueryHandler(on_reminder_back, pattern=r"^rem_back:"))
    app.add_handler(CallbackQueryHandler(on_set_reminder, pattern=r"^set_remind:"))
    
    app.add_handler(CallbackQueryHandler(on_edit_list, pattern=r"^edit_list$"))
    app.add_handler(CallbackQueryHandler(on_edit_select, pattern=r"^edit:"))
    app.add_handler(CallbackQueryHandler(on_edit_back_to_tasks, pattern=r"^edit_back_to_tasks$"))
    
    app.add_handler(CallbackQueryHandler(on_delete_task, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(on_done_task, pattern=r"^done:"))

    # Text Handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Daily Digest Job (07:30 утра)
    if app.job_queue:
        app.job_queue.run_daily(
            send_daily_digest,
            time=dtime(hour=7, minute=30, tzinfo=LOCAL_TZ),
            name="daily_digest",
        )

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()