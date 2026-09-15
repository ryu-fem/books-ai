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
)
from ai_matcher import ai_pick_books, AIMatchUnavailable

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


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# ---------- أوامر المالك ----------

async def add_book_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يُستخدم في الخاص مع المالك فقط:
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

    book_id = db.add_book(title=title, file_id=doc.file_id, file_name=doc.file_name)
    await message.reply_text(f"✅ تمت إضافة الكتاب رقم {book_id}: {title}")


async def handle_owner_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي ملف يبعته المالك في الخاص مع كابشن، يتضاف تلقائي كـ كتاب."""
    user = update.effective_user
    message = update.message
    if not is_owner(user.id) or update.effective_chat.type != ChatType.PRIVATE:
        return
    if not message.document:
        return
    if not message.caption:
        await message.reply_text(
            "من فضلك ابعت الملف تاني وحط اسم الكتاب في الكابشن عشان أقدر أضيفه."
        )
        return

    title = message.caption.strip()
    book_id = db.add_book(title=title, file_id=message.document.file_id,
                           file_name=message.document.file_name)
    await message.reply_text(f"✅ اتضاف الكتاب رقم {book_id}: {title}")


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

    # ترتيب المجموعات: أولى، تانية، تالتة، وأخيرًا غير المصنّف
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

    # ملحوظة: عمدًا من غير parse_mode (Markdown) عشان عناوين الكتب ممكن
    # يكون فيها رموز زي _ أو * بتكسر التنسيق وتخلي الرسالة تفشل بالكامل
    text = "📚 قائمة الكتب:\n\n" + "\n\n".join(sections)
    # تليجرام بيحدد أقصى طول للرسالة (4096 حرف)، لو القائمة كبيرة قوي نقسمها
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


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أنا بوت مكتبة. لو انت المالك ابعتلي كتاب في الخاص مع اسمه في الكابشن.\n"
        "وأي حد في الجروب يتكلم عن كتاب من الكتب المتضافة (بأي صياغة)، "
        "هبعتهوله تلقائي لو لقيت تطابق كويس."
    )


# ---------- المطابقة الذكية في الجروب (بدون كلمة تريجر ثابتة) ----------

async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    text = message.text.strip()
    if len(text) < MIN_MESSAGE_LENGTH:
        return

    books = db.get_all_books()
    if not books:
        return

    # فلترة أولى بالمرحلة الدراسية *والمادة* (لو واضحين في رسالة المستخدم)
    # قبل ما نبعت أي حاجة للـ AI أصلاً - ده بيمنع تمامًا إن الموديل يرجع
    # كتاب من مرحلة أو مادة مختلفة عن اللي المستخدم طلبها. الفلترة دي
    # "متحفظة": لو مقدرناش نحدد المرحلة أو المادة من النص، مبنرفضش الكتاب
    # على أساسها ونسيب القرار للـ AI.
    query_grade = detect_query_grade(text)
    query_subject = detect_subject(text)
    candidate_books = [
        b for b in books
        if grade_matches(detect_grade(b[1]), query_grade)
        and subject_matches(detect_subject(b[1]), query_subject)
    ]

    # لو المستخدم حدد مرحلة أو مادة واضحة ومفيش أي كتاب يطابقها أصلاً،
    # مفيش داعي نكلم الـ AI خالص.
    if (query_grade is not None or query_subject is not None) and not candidate_books:
        return

    # بنستخدم Groq API (مجاني بالكامل) عشان يحدد كل الكتب المطابقة بشرط إن
    # المرحلة الدراسية والمادة الاتنين يتطابقوا مع الطلب. لو الشرط اتحقق،
    # بيرجع كل الكتب المطابقة (كل الأجزاء)، مش كتاب واحد بس. لو الـ AI شغال
    # ورد بقائمة فاضية، ده رد شرعي (مفيش تطابق كافي) وبنسيبه كده. لو الـ AI
    # فشل يشتغل أصلاً (مفتاح مفقود، مشكلة نت..)، بننزل على مطابقة نصية
    # احتياطية (rapidfuzz) بترجع أقرب كتاب واحد بس.
    matched_books = []
    try:
        book_ids = ai_pick_books(text, candidate_books)
        matched_books = [b for b in candidate_books if b[0] in book_ids]
    except AIMatchUnavailable:
        titles = [b[1] for b in candidate_books]
        result = process.extractOne(text, titles, scorer=fuzz.WRatio)
        if result:
            _, score, idx = result
            if score >= MATCH_THRESHOLD:
                matched_books = [candidate_books[idx]]

    # لو مفيش تطابق منطقي، البوت ببساطة مايردش خالص - ده السلوك الطبيعي.
    if not matched_books:
        return

    for book in matched_books:
        book_id, title, file_id, file_name = book
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=file_id,
            caption=f"📖 {title}",
            reply_to_message_id=message.message_id,
        )


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
    app.add_handler(CommandHandler("addbook", add_book_cmd))
    app.add_handler(CommandHandler("listbooks", list_books_cmd))
    app.add_handler(CommandHandler("delbook", delete_book_cmd))
    app.add_handler(CommandHandler("dbinfo", dbinfo_cmd))
    app.add_error_handler(error_handler)

    # رفع ملف من المالك في الخاص = إضافة كتاب تلقائي (لو فيه كابشن)
    app.add_handler(
        MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_owner_upload)
    )

    # أي رسالة نصية في الجروب
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            group_message_handler,
        )
    )

    logger.info("البوت شغال...")
    app.run_polling()


if __name__ == "__main__":
    main()
