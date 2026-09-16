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
        # بتسجل كل طلب بحث (سواء لقى تطابق ولا لأ) عشان نقدر نطلع إحصائيات
        # زي "كام طلب وصلك" و"كام واحد اتلقاله تطابق" - إجمالي أو النهاردة بس.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                matched INTEGER NOT NULL
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


def log_request(matched: bool):
    """بتسجل طلب بحث جديد (سواء لقى تطابق أو لأ) في السجل، عشان تتحسب في
    إحصائيات /requests و /today."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO requests_log (matched) VALUES (?)", (1 if matched else 0,)
        )
        conn.commit()


def count_requests():
    """بترجع (الإجمالي, اللي لقى تطابق, اللي ملقاش) لكل الطلبات المسجلة
    من أول ما البوت اشتغل."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(matched), 0) FROM requests_log")
        total, matched = cur.fetchone()
        return total, matched, total - matched


def count_requests_today():
    """زي count_requests بس للطلبات اللي حصلت النهاردة بس (حسب توقيت
    السيرفر اللي شغال عليه البوت)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(matched), 0) FROM requests_log "
            "WHERE date(ts) = date('now')"
        )
        total, matched = cur.fetchone()
        return total, matched, total - matched
