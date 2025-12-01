import sqlite3
from typing import List, Tuple, Optional

from config import DB_PATH


def init_db():
    """Создаёт таблицу tasks и добавляет недостающие колонки."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Базовая таблица (если нет)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            due_at TEXT,
            notified INTEGER DEFAULT 0
        )
        """
    )

    # На случай, если таблица уже была без новых полей — аккуратный апгрейд:
    cursor.execute("PRAGMA table_info(tasks)")
    cols = [row[1] for row in cursor.fetchall()]

    if "due_at" not in cols:
        cursor.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")

    if "notified" not in cols:
        cursor.execute("ALTER TABLE tasks ADD COLUMN notified INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


def add_task(user_id: int, text: str, due_at_iso: Optional[str] = None):
    """Добавляет задачу в базу. due_at_iso — строка ISO или None."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO tasks (user_id, text, due_at) VALUES (?, ?, ?)",
        (user_id, text, due_at_iso),
    )

    conn.commit()
    conn.close()


def get_tasks(user_id: int) -> List[Tuple[int, str, Optional[str]]]:
    """Возвращает список задач пользователя: (id, text, due_at_iso)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, text, due_at FROM tasks WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def delete_task(user_id: int, task_id: int):
    """Удаляет задачу по ID."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, user_id),
    )

    conn.commit()
    conn.close()
