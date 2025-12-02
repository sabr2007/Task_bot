import sqlite3
from typing import List, Tuple, Optional

from config import DB_PATH


def init_db():
    """Создаёт таблицу tasks и добавляет недостающие колонки."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()


    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            due_at TEXT,
            notified INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            completed_at TEXT
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
    if "status" not in cols:
        cursor.execute("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'active'")

    if "completed_at" not in cols:
        cursor.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")

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
        """
        SELECT id, text, due_at FROM tasks
        WHERE user_id = ?
          AND (status IS NULL OR status = 'active')
        ORDER BY id DESC
        """,
        (user_id,),
    )

def get_users_with_tasks() -> List[int]:
    """Возвращает id пользователей, у которых есть активные задачи."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT DISTINCT user_id
        FROM tasks
        WHERE status IS NULL OR status = 'active'
        """
    )

    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]


    rows = cursor.fetchall()
    conn.close()
    return rows

def get_archived_tasks(user_id: int) -> List[Tuple[int, str, Optional[str]]]:
    """Возвращает список выполненных задач пользователя."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, text, due_at FROM tasks
        WHERE user_id = ?
          AND status = 'done'
        ORDER BY completed_at DESC, id DESC
        """,
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

def set_task_done(user_id: int, task_id: int):
    """Отмечает задачу выполненной."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE tasks
        SET status = 'done',
            completed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND user_id = ?
        """,
        (task_id, user_id),
    )

    conn.commit()
    conn.close()


