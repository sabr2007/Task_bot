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


def add_task(user_id: int, text: str, due_at_iso: Optional[str] = None) -> int:
    """Добавляет задачу в базу и возвращает её ID."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO tasks (user_id, text, due_at) VALUES (?, ?, ?)",
        (user_id, text, due_at_iso),
    )

    task_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return task_id

def get_tasks(user_id: int) -> List[Tuple[int, str, Optional[str]]]:
    """Возвращает список активных задач пользователя: (id, text, due_at_iso)."""
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

    rows = cursor.fetchall()
    conn.close()
    return rows

def get_task(user_id: int, task_id: int) -> Optional[Tuple[int, str, Optional[str]]]:
    """Возвращает одну задачу пользователя по ID или None."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, text, due_at
        FROM tasks
        WHERE id = ? AND user_id = ?
        """,
        (task_id, user_id),
    )

    row = cursor.fetchone()
    conn.close()
    return row

def update_task_due(user_id: int, task_id: int, due_at_iso: Optional[str]):
    """Обновляет дедлайн задачи."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE tasks
        SET due_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (due_at_iso, task_id, user_id),
    )

    conn.commit()
    conn.close()

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

def get_archived_tasks(user_id: int) -> List[Tuple[int, str, Optional[str], Optional[str]]]:
    """Возвращает список выполненных задач пользователя: (id, text, due_at, completed_at)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, text, due_at, completed_at
        FROM tasks
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

def update_task_text(user_id: int, task_id: int, new_text: str):
    """Обновляет текст задачи."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE tasks
        SET text = ?
        WHERE id = ? AND user_id = ?
        """,
        (new_text, task_id, user_id),
    )

    conn.commit()
    conn.close()

