import os
import sqlite3
from contextlib import closing

DB_PATH = os.environ.get("DB_PATH", "books.db")


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_name TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        conn.commit()


def remember_user(user_id: int):
    """بيسجل أي مستخدم كلم البوت في الخاص، عشان نقدر نستخدمه بعدين في
    أمر /broadcast. لو المستخدم مسجل قبل كده، مفيش تكرار (PRIMARY KEY)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        conn.commit()


def get_all_user_ids():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("SELECT user_id FROM users")
        return [row[0] for row in cur.fetchall()]


def count_users() -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM users")
        return cur.fetchone()[0]


def add_book(title: str, file_id: str, file_name: str = None) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO books (title, file_id, file_name) VALUES (?, ?, ?)",
            (title.strip(), file_id, file_name),
        )
        conn.commit()
        return cur.lastrowid


def delete_book(book_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.commit()
        return cur.rowcount > 0


def get_all_books():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("SELECT id, title, file_id, file_name FROM books ORDER BY id")
        return cur.fetchall()


def get_book_by_id(book_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT id, title, file_id, file_name FROM books WHERE id = ?", (book_id,)
        )
        return cur.fetchone()
