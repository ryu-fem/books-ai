import asyncio
import logging
import os

from rapidfuzz import process, fuzz
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import database as db
from grading import (
    detect_grade,
    detect_query_grade,
    grade_matches,
    detect_subject,
    subject_matches,
    normalize_text,
)
from ai_matcher import (
    ai_pick_books,
    AIMatchUnavailable,
    has_book_word,
    might_be_book_request,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==== الإعدادات ====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ضع_التوكن_هنا")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))  # ايدي التليجرام بتاعك

# أقل عدد أحرف في الرسالة عشان تتفحص أصلاً (تقليل الضوضاء من رسائل زي "تمام"، "ok"..)
MIN_MESSAGE_LENGTH = 4

MATCH_THRESHOLD = 85  # نسبة التشابه المطلوبة في المطابقة الاحتياطية (rapidfuzz) لو الـ AI فشل يشتغل
# (رقم عالي عمدًا عشان يقلل الأخطاء زي مطابقة كتابين مختلفين بس بينهم كلمة
# مشتركة زي "بكالوريا" أو "ثانوي" - المفروض الاحتياطي ده نادر الاستخدام
# أصلاً، الاعتماد الحقيقي على الـ AI في ai_matcher.py)

DUPLICATE_THRESHOLD = 90  # نسبة التشابه اللي لو كتاب جديد وصلها مع كتاب
# موجود قبل كده، بنعتبره "نفس الكتاب تقريبًا" ونحذّر المالك (من غير ما نمنعه)

# طلبات إضافة الكتب من المستخدمين
MAX_PENDING_SUBMISSIONS = 5  # أقصى عدد طلبات معلقة للمستخدم الواحد (ضد السبام)
MAX_TITLE_LENGTH = 200
MAX_REASON_LENGTH = 500

# رسالة التعليمات الثابتة (بتتبعت في /start وفي الخاص لو الرسالة مش طلب كتاب)
HELP_TEXT = (
    "أهلاً! أنا بوت مكتبة 📚\n"
    "اكتب اسم الكتاب اللي عايزه (أو المرحلة والمادة، أو اسم المؤلف) وهبعتهولك لو موجود.\n\n"
    "📥 عندك كتاب مش موجود عندنا؟ ابعتهولي هنا في الخاص كملف مع اسمه في الكابشن، "
    "وهتتم مراجعته من الإدارة."
)


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def _user_label(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


# ---------- إضافة كتاب (فحص التكرار من غير ما نمنع الإضافة) ----------

def find_duplicate_book(title: str):
    """بيدور على كتاب موجود قبل كده باسم شبيه قوي (مش لازم متطابق حرفيًا)
    عشان نلفت نظر المالك لاحتمال التكرار. بيرجع (id, title, file_id,
    file_name) لو لقى حاجة، أو None لو مفيش تشابه كافي."""
    books = db.get_all_books()
    if not books:
        return None
    titles = [b[1] for b in books]
    result = process.extractOne(title, titles, scorer=fuzz.WRatio)
    if result:
        _, score, idx = result
        if score >= DUPLICATE_THRESHOLD:
            return books[idx]
    return None


async def handle_new_book(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           title: str, file_id: str, file_name: str):
    """نقطة مركزية لإضافة أي كتاب جديد (سواء من /addbook أو رفع مباشر من
    المالك، أو /accept على طلب مستخدم). بنضيف الكتاب على طول، ولو لقينا
    كتاب شبيه قوي موجود قبل كده، بنحذّر المالك بس في نفس الرسالة (من غير
    ما نمنع الإضافة أو نسأله بأزرار) - يقدر يمسح القديم بـ /delbook لو حابب."""
    duplicate = find_duplicate_book(title)
    book_id = db.add_book(title=title, file_id=file_id, file_name=file_name)
    msg = f"✅ تمت إضافة الكتاب رقم {book_id}: {title}"
    if duplicate:
        dup_id, dup_title = duplicate[0], duplicate[1]
        msg += (
            f"\n\n⚠️ ملحوظة: لقيت كتاب شبيه موجود قبل كده:\n"
            f"#{dup_id} - {dup_title}\n"
            f"لو عايز تمسح القديم: /delbook {dup_id}"
        )
    await update.message.reply_text(msg)


# ---------- طلبات إضافة كتب من المستخدمين (بأوامر بس، من غير أزرار) ----------
# المسار: مستخدم يبعت ملف في الخاص -> يتسجل كطلب معلّق ويوصل تنبيه نصي
# للمالك فيه رقم الطلب -> المالك يستخدم /accept رقم [اسم بديل] أو
# /reject رقم [سبب] عشان يحسم الطلب.

def _submission_summary(sub) -> str:
    """نص وصف الطلب. sub = صف من db.get_submission."""
    sub_id, user_id, username, title, file_id, file_name, status = sub
    return (
        f"#{sub_id} - {title}\n"
        f"من: {username} (ID: {user_id})\n"
        f"الملف: {file_name or '-'}\n"
        f"الحالة: {status}"
    )


async def handle_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي مستخدم (مش المالك) بيبعت ملف في الخاص = طلب إضافة كتاب للمراجعة."""
    user, message = update.effective_user, update.message
    doc = message.document
    db.remember_user(user.id)

    if not OWNER_ID:
        await message.reply_text("⚠️ استقبال الكتب مش متاح دلوقتي.")
        return
    if db.count_pending_submissions(user.id) >= MAX_PENDING_SUBMISSIONS:
        await message.reply_text(
            f"⏳ عندك {MAX_PENDING_SUBMISSIONS} طلبات لسه بتتراجع. "
            "استنى لما الإدارة ترد عليهم وبعدين ابعت تاني."
        )
        return

    title = (message.caption or "").strip()
    if not title:  # مفيش كابشن -> نستخدم اسم الملف من غير الامتداد
        title = os.path.splitext(doc.file_name or "")[0].replace("_", " ").strip()
    title = title[:MAX_TITLE_LENGTH]
    if not title:
        await message.reply_text("من فضلك ابعت الملف تاني وحط اسم الكتاب في الكابشن.")
        return

    sub_id = db.add_submission(user.id, _user_label(user), title, doc.file_id, doc.file_name)
    similar = find_duplicate_book(title)

    owner_text = (
        f"📥 طلب إضافة كتاب #{sub_id}\n\n"
        f"📖 الاسم المقترح: {title}\n"
        f"👤 من: {_user_label(user)} (ID: {user.id})\n"
        f"📎 الملف: {doc.file_name or '-'}"
    )
    if similar:
        owner_text += f"\n⚠️ شبيه بكتاب موجود: #{similar[0]} - {similar[1]}"
    owner_text += (
        f"\n\n✅ للقبول: /accept {sub_id} [اسم بديل اختياري]"
        f"\n❌ للرفض: /reject {sub_id} [سبب اختياري]"
    )

    try:
        await context.bot.send_document(
            chat_id=OWNER_ID,
            document=doc.file_id,
            caption=owner_text[:1024],
        )
    except Exception:
        logger.exception("فشل إرسال طلب إضافة كتاب للمالك")
        await message.reply_text("⚠️ حصلت مشكلة وأنا بوصّل طلبك للإدارة، جرّب تاني بعد شوية.")
        return

    await message.reply_text(
        f"✅ استلمت الكتاب \"{title}\" وبعتّه للإدارة للمراجعة. "
        "هبلغك أول ما يتم القبول أو الرفض."
    )


async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: /accept رقم_الطلب [اسم بديل اختياري]
    لو معملتش اسم بديل، بيستخدم الاسم اللي المستخدم اقترحه."""
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("استخدم: /accept رقم_الطلب [اسم بديل اختياري]")
        return
    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("رقم الطلب لازم يكون رقم.")
        return

    sub = db.get_submission(sub_id)
    if not sub or sub[6] != "pending":
        await update.message.reply_text("⏳ الطلب ده مش موجود أو اتعالج قبل كده.")
        return

    override_name = " ".join(context.args[1:]).strip()
    name = (override_name or sub[3])[:MAX_TITLE_LENGTH]
    book_id = db.add_book(title=name, file_id=sub[4], file_name=sub[5])
    db.resolve_submission(sub_id, "accepted")

    try:
        await context.bot.send_message(
            chat_id=sub[1],
            text=f"🎉 الكتاب اللي بعتّه اتقبل واتضاف للمكتبة باسم:\n{name}",
        )
        notified = True
    except Exception:
        logger.warning("مقدرتش أبلغ المستخدم %s بقبول طلبه #%s", sub[1], sub_id)
        notified = False

    result = f"✅ اتقبل واتضاف كتاب رقم {book_id}: {name}"
    if not notified:
        result += "\n⚠️ مقدرتش أبلغ المستخدم (ممكن يكون عمل بلوك للبوت)."
    await update.message.reply_text(result)


async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: /reject رقم_الطلب [سبب اختياري]"""
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("استخدم: /reject رقم_الطلب [سبب اختياري]")
        return
    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("رقم الطلب لازم يكون رقم.")
        return

    sub = db.get_submission(sub_id)
    if not sub or sub[6] != "pending":
        await update.message.reply_text("⏳ الطلب ده مش موجود أو اتعالج قبل كده.")
        return

    reason = " ".join(context.args[1:]).strip()[:MAX_REASON_LENGTH]
    db.resolve_submission(sub_id, "rejected")

    user_msg = f"😔 للأسف الكتاب اللي بعتّه ({sub[3]}) اترفض."
    if reason:
        user_msg += f"\nالسبب: {reason}"
    try:
        await context.bot.send_message(chat_id=sub[1], text=user_msg)
        notified = True
    except Exception:
        logger.warning("مقدرتش أبلغ المستخدم %s برفض طلبه #%s", sub[1], sub_id)
        notified = False

    result = f"❌ اترفض الطلب #{sub_id}" + (f"\nالسبب: {reason}" if reason else " (من غير سبب)")
    if not notified:
        result += "\n⚠️ مقدرتش أبلغ المستخدم (ممكن يكون عمل بلوك للبوت)."
    await update.message.reply_text(result)


async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: /subs - بيعرض كل طلبات إضافة الكتب المعلّقة."""
    if not is_owner(update.effective_user.id):
        return
    pending = db.get_pending_submissions()
    if not pending:
        await update.message.reply_text("مفيش طلبات معلّقة دلوقتي.")
        return
    lines = [_submission_summary(sub) for sub in pending]
    text = "📥 الطلبات المعلّقة:\n\n" + "\n\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


# ---------- أوامر المالك: الكتب ----------

async def add_book_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يُستخدم في الخاص مع المالك بس:
    ترفع ملف الكتاب (PDF مثلا) مع كابشن هو اسم الكتاب، أو ترد على الملف بالأمر:
    /addbook اسم الكتاب
    """
    user = update.effective_user
    if not is_owner(user.id):
        return

    message = update.message
    doc = None
    title = None

    if message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document
        title = " ".join(context.args) if context.args else doc.file_name
    elif message.document:
        doc = message.document
        title = message.caption or doc.file_name

    if not doc:
        await message.reply_text(
            "ابعت الملف وحط اسم الكتاب في الكابشن، أو رد على الملف بالأمر /addbook اسم الكتاب"
        )
        return

    await handle_new_book(update, context, title, doc.file_id, doc.file_name)


async def handle_owner_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي ملف يبعته المالك في الخاص مع كابشن، يتضاف تلقائي كـ كتاب. أي مستخدم
    تاني بيبعت ملف في الخاص، بيتعامل معاه كطلب إضافة كتاب (للمراجعة)."""
    user = update.effective_user
    message = update.message
    if update.effective_chat.type != ChatType.PRIVATE or not message.document:
        return
    if not is_owner(user.id):
        await handle_submission(update, context)
        return
    if not message.caption:
        await message.reply_text(
            "من فضلك ابعت الملف تاني وحط اسم الكتاب في الكابشن عشان أقدر أضيفه."
        )
        return

    title = message.caption.strip()
    await handle_new_book(update, context, title, message.document.file_id,
                           message.document.file_name)


async def list_books_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    books = db.get_all_books()
    if not books:
        await update.message.reply_text("لسه مفيش كتب متضافة.")
        return

    grouped = {}
    for b in books:
        book_id, title = b[0], b[1]
        grade = detect_grade(title)
        grouped.setdefault(grade, []).append((book_id, title))

    order = [
        "أولى ثانوي",
        "تانية ثانوي (مش بكالوريا)",
        "تانية ثانوي (بكالوريا)",
        "تانية ثانوي",
        "تالتة ثانوي",
        "غير مصنّف",
    ]
    icons = {
        "أولى ثانوي": "1️⃣",
        "تانية ثانوي (مش بكالوريا)": "2️⃣",
        "تانية ثانوي (بكالوريا)": "2️⃣",
        "تانية ثانوي": "2️⃣",
        "تالتة ثانوي": "3️⃣",
        "غير مصنّف": "❓",
    }
    sections = []
    for grade in order:
        if grade in grouped:
            lines = "\n".join(f"{bid}. {t}" for bid, t in grouped[grade])
            sections.append(f"{icons[grade]} {grade}\n{'-' * 20}\n{lines}")
    # باقي المراحل (ابتدائي/إعدادي) اللي مش في الترتيب الثابت فوق
    for grade, items in grouped.items():
        if grade not in order:
            lines = "\n".join(f"{bid}. {t}" for bid, t in items)
            sections.append(f"📘 {grade}\n{'-' * 20}\n{lines}")

    # ملحوظة: عمدًا من غير parse_mode (Markdown) عشان عناوين الكتب ممكن
    # يكون فيها رموز زي _ أو * بتكسر التنسيق وتخلي الرسالة تفشل بالكامل
    text = "📚 قائمة الكتب:\n\n" + "\n\n".join(sections)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def delete_book_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("استخدم: /delbook رقم_الكتاب")
        return
    try:
        book_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("رقم الكتاب لازم يكون رقم.")
        return
    if db.delete_book(book_id):
        await update.message.reply_text("🗑️ تم الحذف.")
    else:
        await update.message.reply_text("مفيش كتاب بالرقم ده.")


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: /search كلمة - بيدور يدوي في عناوين الكتب المتضافة."""
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("استخدم: /search كلمة أو جزء من اسم الكتاب")
        return

    keyword = " ".join(context.args)
    books = db.get_all_books()
    if not books:
        await update.message.reply_text("لسه مفيش كتب متضافة.")
        return

    titles = [b[1] for b in books]
    results = process.extract(keyword, titles, scorer=fuzz.WRatio, limit=10, score_cutoff=60)
    if not results:
        await update.message.reply_text("مفيش أي كتاب قريب من الكلمة دي.")
        return

    lines = [f"{books[idx][0]}. {books[idx][1]} ({int(score)}%)" for _, score, idx in results]
    await update.message.reply_text("🔍 نتائج البحث:\n\n" + "\n".join(lines))


async def find_duplicates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: بيفحص كل الكتب المتضافة ويجمع اللي أسماءهم شبه
    بعض قوي في مجموعات، عشان تقدر تكتشف كتب اتضافت مرتين بالغلط."""
    if not is_owner(update.effective_user.id):
        return

    books = db.get_all_books()
    if len(books) < 2:
        await update.message.reply_text("مفيش كتب كفاية عشان نقارن.")
        return

    groups = []
    used = set()
    for i, book_a in enumerate(books):
        if book_a[0] in used:
            continue
        group = [book_a]
        for book_b in books[i + 1:]:
            if book_b[0] in used:
                continue
            score = fuzz.WRatio(book_a[1], book_b[1])
            if score >= DUPLICATE_THRESHOLD:
                group.append(book_b)
                used.add(book_b[0])
        if len(group) > 1:
            used.add(book_a[0])
            groups.append(group)

    if not groups:
        await update.message.reply_text("✅ مفيش أي كتب متكررة أو متشابهة قوي.")
        return

    lines = [f"⚠️ لقيت {len(groups)} مجموعة كتب شبه بعض:\n"]
    for idx, group in enumerate(groups, start=1):
        lines.append(f"مجموعة {idx}:")
        for b in group:
            lines.append(f"  #{b[0]} - {b[1]}")
        lines.append("")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


# ---------- أوامر عامة وتشخيصية ----------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = HELP_TEXT
    if is_owner(update.effective_user.id):
        text += "\n\n👑 وانت المالك: ابعت /help عشان تشوف كل الأوامر."
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: بيجمع كل أوامر المالك في رسالة واحدة."""
    if not is_owner(update.effective_user.id):
        return
    text = (
        "🛠️ أوامر المالك:\n\n"
        "📚 الكتب\n"
        "/addbook اسم الكتاب - يضيف كتاب (رد على الملف بالأمر ده)\n"
        "/listbooks - يعرض كل الكتب مجمّعة بالمرحلة\n"
        "/delbook رقم - يمسح كتاب برقمه\n"
        "/search كلمة - يدور يدوي في الكتب باسم أو جزء منه\n"
        "/dupes - يفحص الكتب المتشابهة قوي (احتمال تكرار)\n\n"
        "📥 طلبات المستخدمين\n"
        "/subs - يعرض كل طلبات إضافة الكتب المعلّقة\n"
        "/accept رقم [اسم بديل] - يقبل طلب ويضيف الكتاب\n"
        "/reject رقم [سبب] - يرفض طلب\n"
        "/reply رقم نص الرد - يرد على شخص سأل عن كتاب مش موجود\n\n"
        "📊 الإحصائيات\n"
        "/stats - عدد الكتب والمستخدمين\n"
        "/requests - إجمالي عدد طلبات البحث من الأول (لقت/ملقتش)\n"
        "/today - طلبات البحث النهاردة بس\n\n"
        "📢 التواصل\n"
        "/broadcast رسالتك - يبعت رسالة لكل اللي كلموا البوت في الخاص\n\n"
        "🔧 تشخيص\n"
        "/dbinfo - معلومات عن قاعدة البيانات ومكانها\n"
        "/backup - يبعتلك نسخة احتياطية من قاعدة البيانات"
    )
    await update.message.reply_text(text)


async def dbinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر تشخيصي للمالك بس: بيوري المسار الحقيقي لقاعدة البيانات جوه
    الكونتينر، عشان نتأكد هل الـ Volume شغال فعلاً ولا لأ."""
    if not is_owner(update.effective_user.id):
        return

    db_path_env = os.environ.get("DB_PATH", "(مش متظبط - بيستخدم books.db الافتراضي)")
    abs_path = os.path.abspath(db.DB_PATH)
    dir_path = os.path.dirname(abs_path) or "."
    dir_exists = os.path.isdir(dir_path)
    file_exists = os.path.isfile(abs_path)
    file_size = os.path.getsize(abs_path) if file_exists else 0

    dir_listing = ""
    if dir_exists:
        try:
            entries = os.listdir(dir_path)
            dir_listing = "\n".join(entries) if entries else "(فاضي)"
        except Exception as exc:
            dir_listing = f"(مقدرش أقرأ المجلد: {exc})"

    books_count = len(db.get_all_books())
    text = (
        "🔍 معلومات قاعدة البيانات:\n\n"
        f"DB_PATH env var: {db_path_env}\n"
        f"المسار الفعلي: {abs_path}\n"
        f"المجلد ({dir_path}) موجود؟ {'✅ آه' if dir_exists else '❌ لأ'}\n"
        f"محتويات المجلد:\n{dir_listing}\n\n"
        f"ملف قاعدة البيانات موجود؟ {'✅ آه' if file_exists else '❌ لأ'}\n"
        f"حجم الملف: {file_size} بايت\n"
        f"عدد الكتب المتضافة حاليًا: {books_count}"
    )
    await update.message.reply_text(text)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    books_count = len(db.get_all_books())
    users_count = db.count_users()
    await update.message.reply_text(
        f"📊 إحصائيات:\n\n📚 عدد الكتب: {books_count}\n"
        f"👤 عدد المستخدمين اللي كلموا البوت في الخاص: {users_count}"
    )


async def requests_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    total, matched, not_matched = db.count_requests()
    await update.message.reply_text(
        "📈 إجمالي الطلبات من الأول:\n\n"
        f"📨 كل الطلبات: {total}\n"
        f"✅ لقيت تطابق واتبعت: {matched}\n"
        f"😔 ملقتش تطابق: {not_matched}"
    )


async def today_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    total, matched, not_matched = db.count_requests_today()
    await update.message.reply_text(
        "📅 طلبات النهاردة:\n\n"
        f"📨 كل الطلبات: {total}\n"
        f"✅ لقيت تطابق واتبعت: {matched}\n"
        f"😔 ملقتش تطابق: {not_matched}"
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("استخدم: /broadcast الرسالة اللي عايز تبعتها")
        return

    text = update.message.text.split(maxsplit=1)[1]
    user_ids = db.get_all_user_ids()
    if not user_ids:
        await update.message.reply_text("مفيش أي مستخدم كلم البوت في الخاص لسه.")
        return

    sent, failed = 0, 0
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ اتبعتت لـ {sent} مستخدم. فشلت مع {failed}.")


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not os.path.isfile(db.DB_PATH):
        await update.message.reply_text("ملف قاعدة البيانات مش موجود.")
        return
    await update.message.reply_document(
        document=open(db.DB_PATH, "rb"),
        filename=os.path.basename(db.DB_PATH),
        caption="📦 نسخة احتياطية من قاعدة البيانات",
    )


# ---------- الرد على شخص سأل عن كتاب مش موجود (بأمر /reply بس) ----------

async def notify_not_found(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    """بترد على الشخص إن الكتاب مش متوفر، وتسجل الطلب في السجل، وتبلغ
    المالك بيه مع رقم يقدر يرد بيه بأمر /reply. مبنبلغش المالك لو هو نفسه
    اللي بيدور (زي لما يجرب يبحث بنفسه)."""
    await update.message.reply_text(
        "😔 الكتاب ده مش متوفر حاليًا.\n"
        "📥 لو معاك نسخة منه، ابعتهالي في الخاص كملف مع اسمه في الكابشن وهتتراجع من الإدارة."
    )
    db.log_request(matched=False)

    user = update.effective_user
    if not OWNER_ID or user.id == OWNER_ID:
        return

    chat = update.effective_chat
    chat_label = "الخاص" if chat.type == ChatType.PRIVATE else (chat.title or "جروب")
    username = _user_label(user)

    reply_id = db.add_pending_reply(
        chat_id=chat.id, message_id=update.message.message_id,
        user_label=username, query_text=query_text,
    )
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "📩 حد سأل عن كتاب مش موجود عندك:\n\n"
                f"من: {username}\n"
                f"مكان الرسالة: {chat_label}\n"
                f"النص: {query_text}\n\n"
                f"عشان ترد عليه: /reply {reply_id} نص ردك"
            ),
        )
    except Exception:
        logger.exception("فشل إرسال تنبيه للمالك عن كتاب مش متوفر")


async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: /reply رقم نص الرد - بيبعت نص الرد للشخص اللي سأل
    عن كتاب مش موجود، كـ reply على رسالته الأصلية في نفس الشات بتاعه."""
    if not is_owner(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("استخدم: /reply رقم نص الرد")
        return
    try:
        reply_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("رقم الطلب لازم يكون رقم.")
        return

    target = db.get_pending_reply(reply_id)
    if not target or target[5]:  # target[5] = resolved
        await update.message.reply_text("⏳ الطلب ده مش موجود أو اترد عليه قبل كده.")
        return

    text = update.message.text.split(maxsplit=2)[2]
    _, chat_id, message_id, user_label, _query_text, _resolved = target
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=message_id)
        db.resolve_pending_reply(reply_id)
        await update.message.reply_text(f"✅ اتبعتت لـ {user_label}.")
    except Exception:
        logger.exception("فشل إرسال رد المالك للشخص")
        await update.message.reply_text("❌ حصلت مشكلة وأنا بابعت الرد (ممكن يكون عمل بلوك للبوت).")


async def notify_owner_success(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                query_text: str, matched_books: list):
    """بتبلغ المالك إن طلب حد اتحقق فعلاً وابعتله كتاب/كتب تلقائي، عشان
    يقدر يتابع الطلبات اللي البوت بيردها من غير تدخله. مبنبلغش المالك لو
    هو نفسه اللي بيدور."""
    user = update.effective_user
    if not OWNER_ID or user.id == OWNER_ID:
        return

    chat = update.effective_chat
    chat_label = "الخاص" if chat.type == ChatType.PRIVATE else (chat.title or "جروب")
    username = _user_label(user)
    titles = "\n".join(f"- {b[1]}" for b in matched_books)
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "✅ اتبعت كتاب تلقائي لحد سأل عنه:\n\n"
                f"من: {username}\n"
                f"مكان الرسالة: {chat_label}\n"
                f"النص: {query_text}\n\n"
                f"الكتب اللي اتبعتت:\n{titles}"
            ),
        )
    except Exception:
        logger.exception("فشل إرسال تنبيه للمالك عن كتاب اتبعت بنجاح")


async def reply_not_a_book(update: Update):
    """الرسالة مش طلب كتاب: في الجروب بنتجاهلها تمامًا. في الخاص بس بنرد
    بتعليمات الاستخدام الثابتة (مفيش أي كتب ولا كلام من الـ AI)."""
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(HELP_TEXT)


# ---------- المطابقة الذكية في الجروب وفي الخاص (بدون كلمة تريجر ثابتة) ----------

async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    chat_type = update.effective_chat.type
    if chat_type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE):
        return

    if chat_type == ChatType.PRIVATE:
        # لو حد كلم البوت في الخاص، بنسجله عشان نقدر نستخدم /broadcast بعدين
        db.remember_user(update.effective_user.id)

    text = message.text.strip()
    if len(text) < MIN_MESSAGE_LENGTH:
        return

    books = db.get_all_books()

    # فلتر أولي رخيص: لو الرسالة مفيهاش أي علامة إنها طلب كتاب (كلمة كتاب/
    # طلب، مرحلة/مادة، أو شبه اسم كتاب عندنا) مبنكلمش الـ AI ولا بنرد.
    if not might_be_book_request(text, books):
        await reply_not_a_book(update)
        return

    # فلترة أولى بالمرحلة الدراسية *والمادة* (لو واضحين في رسالة المستخدم)
    # قبل ما نبعت أي حاجة للـ AI - ده بيمنع تمامًا إن الموديل يرجع كتاب من
    # مرحلة أو مادة مختلفة عن اللي المستخدم طلبها.
    query_grade = detect_query_grade(text)
    query_subject = detect_subject(text)
    candidate_books = [
        b for b in books
        if grade_matches(detect_grade(b[1]), query_grade)
        and subject_matches(detect_subject(b[1]), query_subject)
    ]

    # الـ AI بيقرر الأول هل الرسالة طلب كتاب أصلاً (None = لأ)، وبعدين بيختار
    # الكتب المطابقة من candidate_books بس (كلها من القاعدة، مفيش كتب متألّفة).
    # الطلب بيتنفذ في thread عشان الاتصال بالـ API مايوقفش البوت كله.
    matched_books = []
    try:
        book_ids = await asyncio.to_thread(ai_pick_books, text, candidate_books)
    except AIMatchUnavailable as exc:
        logger.warning("الـ AI مش متاح (%s) - هنستخدم المطابقة الاحتياطية", exc)
        titles = [b[1] for b in candidate_books]
        result = process.extractOne(
            text, titles, scorer=fuzz.WRatio, processor=normalize_text
        ) if titles else None
        if result:
            _, score, idx = result
            if score >= MATCH_THRESHOLD:
                matched_books = [candidate_books[idx]]
        # من غير الـ AI مش قادرين نتأكد إنها طلب كتاب، فمانبلغش "مش متوفر" ولا
        # نبعت تنبيه للمالك غير لو فيها كلمة صريحة زي "كتاب"
        if not matched_books and not has_book_word(text):
            return
    else:
        if book_ids is None:
            await reply_not_a_book(update)
            return
        matched_books = [b for b in candidate_books if b[0] in book_ids]

    if not matched_books:
        await notify_not_found(update, context, text)
        return

    for book in matched_books:
        book_id, title, file_id, file_name = book
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=file_id,
            caption=f"📖 {title}",
            reply_to_message_id=message.message_id,
        )

    db.log_request(matched=True)
    await notify_owner_success(update, context, text, matched_books)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """بيسجّل أي خطأ يحصل في أي handler في اللوجز، عشان مانفضلش من غير ما
    نعرف السبب لما حاجة توقف عن الرد فجأة."""
    logger.exception("حصل خطأ غير متوقع", exc_info=context.error)


def main():
    if BOT_TOKEN == "ضع_التوكن_هنا" or OWNER_ID == 0:
        print("⚠️ لازم تحط BOT_TOKEN و OWNER_ID كمتغيرات بيئة قبل التشغيل.")

    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("addbook", add_book_cmd))
    app.add_handler(CommandHandler("listbooks", list_books_cmd))
    app.add_handler(CommandHandler("delbook", delete_book_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("dbinfo", dbinfo_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("requests", requests_stats_cmd))
    app.add_handler(CommandHandler("today", today_stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("dupes", find_duplicates_cmd))
    app.add_handler(CommandHandler("subs", subs_cmd))
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))
    app.add_handler(CommandHandler("reply", reply_cmd))
    app.add_error_handler(error_handler)

    # رفع ملف من المالك في الخاص = إضافة كتاب تلقائي (لو فيه كابشن).
    # رفع ملف من أي حد تاني في الخاص = طلب إضافة كتاب للمراجعة.
    app.add_handler(
        MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_owner_upload)
    )

    # أي رسالة نصية في الجروب أو في الخاص
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUPS | filters.ChatType.PRIVATE),
            group_message_handler,
        )
    )

    logger.info("البوت شغال...")
    app.run_polling()


if __name__ == "__main__":
    main()
