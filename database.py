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
        # طلبات إضافة الكتب اللي المستخدمين بيبعتوها للمراجعة (pending ->
        # accepted / rejected)، بيتقفلوا بأوامر /accept و /reject بس - مفيش
        # أزرار ولا حوارات متعددة الخطوات.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                title TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_name TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
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
        # لما حد يسأل عن كتاب مش موجود، بنسجل مكان رسالته هنا عشان المالك
        # يقدر يرد عليه بأمر /reply رقم نص الرد، من غير ما يحتاج يعرف
        # الشات أو الآيدي بتاعه بنفسه.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_label TEXT,
                query_text TEXT,
                resolved INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def remember_user(user_id: int):
    """بيسجل أي مستخدم كلم البوت في الخاص، عشان نقدر نستخدمه بعدين في
    أمر /broadcast. لو المستخدم مسجل قبل كده، مفيش تكرار (PRIMARY KEY)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
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


# ---------- طلبات إضافة الكتب (Submissions) ----------

def add_submission(user_id: int, username: str, title: str, file_id: str,
                    file_name: str = None) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO submissions (user_id, username, title, file_id, file_name)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, username, title.strip(), file_id, file_name),
        )
        conn.commit()
        return cur.lastrowid


def get_submission(sub_id: int):
    """بيرجع (id, user_id, username, title, file_id, file_name, status) أو None."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT id, user_id, username, title, file_id, file_name, status"
            " FROM submissions WHERE id = ?",
            (sub_id,),
        )
        return cur.fetchone()


def get_pending_submissions():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT id, user_id, username, title, file_id, file_name, status"
            " FROM submissions WHERE status = 'pending' ORDER BY id"
        )
        return cur.fetchall()


def count_pending_submissions(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        )
        return cur.fetchone()[0]


def resolve_submission(sub_id: int, status: str) -> bool:
    """بيغيّر حالة الطلب من pending لـ status، وبيرجع False لو الطلب اتعالج
    قبل كده (عشان مايتقبلش أو يترفض مرتين)."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "UPDATE submissions SET status = ? WHERE id = ? AND status = 'pending'",
            (status, sub_id),
        )
        conn.commit()
        return cur.rowcount > 0


# ---------- إحصائيات طلبات البحث ----------

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


# ---------- طلبات الرد على شخص سأل عن كتاب مش موجود ----------

def add_pending_reply(chat_id: int, message_id: int, user_label: str, query_text: str) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO pending_replies (chat_id, message_id, user_label, query_text)"
            " VALUES (?, ?, ?, ?)",
            (chat_id, message_id, user_label, query_text),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_reply(reply_id: int):
    """بيرجع (id, chat_id, message_id, user_label, query_text, resolved) أو None."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "SELECT id, chat_id, message_id, user_label, query_text, resolved"
            " FROM pending_replies WHERE id = ?",
            (reply_id,),
        )
        return cur.fetchone()


def resolve_pending_reply(reply_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "UPDATE pending_replies SET resolved = 1 WHERE id = ? AND resolved = 0",
            (reply_id,),
        )
        conn.commit()
        return cur.rowcount > 0
