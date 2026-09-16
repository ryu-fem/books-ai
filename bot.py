import logging
import os

from rapidfuzz import process, fuzz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

DUPLICATE_THRESHOLD = 90  # نسبة التشابه اللي لو كتاب جديد وصلها مع كتاب
# موجود قبل كده، بنعتبره "نفس الكتاب تقريبًا" ونسأل المالك يعمل ايه


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def find_duplicate_book(title: str):
    """بيدور على كتاب موجود قبل كده باسم شبيه قوي (مش لازم متطابق حرفيًا)
    عشان نمنع تكرار نفس الكتاب بالغلط. بيرجع (id, title, file_id,
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
    """نقطة مركزية لإضافة أي كتاب جديد (سواء من /addbook أو رفع مباشر).
    لو لقينا كتاب شبيه قوي موجود قبل كده، منضيفوش على طول - بنسأل المالك
    الأول عايز يسيب الاتنين ولا يمسح القديم ويستبدله بالجديد."""
    duplicate = find_duplicate_book(title)
    if duplicate:
        dup_id, dup_title = duplicate[0], duplicate[1]
        key = f"{update.effective_chat.id}_{update.message.message_id}"
        context.bot_data.setdefault("pending_duplicates", {})[key] = {
            "title": title, "file_id": file_id, "file_name": file_name,
            "old_id": dup_id,
        }
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ سيبهم الاتنين", callback_data=f"dup_keep:{key}")],
            [InlineKeyboardButton("🗑️ امسح القديم واستبدله", callback_data=f"dup_replace:{key}")],
        ])
        await update.message.reply_text(
            "⚠️ لقيت كتاب شبيه موجود قبل كده:\n"
            f"#{dup_id} - {dup_title}\n\n"
            f"الكتاب الجديد: {title}\n\n"
            "عايز تعمل ايه؟",
            reply_markup=keyboard,
        )
        return

    book_id = db.add_book(title=title, file_id=file_id, file_name=file_name)
    await update.message.reply_text(f"✅ تمت إضافة الكتاب رقم {book_id}: {title}")


async def duplicate_decision_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بيتنفذ لما المالك يدوس على زرار (سيبهم الاتنين / امسح القديم) بعد
    ما اكتشفنا كتاب شبيه."""
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    action, _, key = query.data.partition(":")
    pending = context.bot_data.get("pending_duplicates", {})
    data = pending.pop(key, None)
    if not data:
        await query.edit_message_text("⏳ الطلب ده اتعمل فيه حاجة قبل كده أو منتهي.")
        return

    if action == "dup_replace":
        db.delete_book(data["old_id"])

    book_id = db.add_book(title=data["title"], file_id=data["file_id"],
                           file_name=data["file_name"])
    if action == "dup_replace":
        await query.edit_message_text(
            f"🗑️➡️✅ اتمسح القديم واتضاف الكتاب الجديد رقم {book_id}: {data['title']}"
        )
    else:
        await query.edit_message_text(
            f"✅ اتضاف الكتاب الجديد رقم {book_id}: {data['title']} (والقديم لسه موجود)"
        )


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

    await handle_new_book(update, context, title, doc.file_id, doc.file_name)


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


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: بيوري عدد الكتب وعدد المستخدمين اللي كلموا البوت
    في الخاص (اللي هيوصلهم أي /broadcast)."""
    if not is_owner(update.effective_user.id):
        return
    books_count = len(db.get_all_books())
    users_count = db.count_users()
    await update.message.reply_text(
        f"📊 إحصائيات:\n\n📚 عدد الكتب: {books_count}\n👤 عدد المستخدمين اللي كلموا البوت في الخاص: {users_count}"
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: /broadcast الرسالة اللي عايز تبعتها لكل اللي كلموا
    البوت في الخاص قبل كده. لازم تحط نص بعد الأمر."""
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
            # ممكن يكون المستخدم عمل بلوك للبوت أو حظره، متجاهلينه ومكملين
            failed += 1

    await update.message.reply_text(f"✅ اتبعتت لـ {sent} مستخدم. فشلت مع {failed}.")


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: بيبعتله ملف قاعدة البيانات (books.db) كنسخة احتياطية."""
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


async def find_duplicates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر للمالك بس: بيفحص كل الكتب المتضافة ويجمع اللي أسماءهم شبه
    بعض قوي (بنفس نسبة التشابه المستخدمة وقت رفع كتاب جديد) في مجموعات،
    عشان تقدر تكتشف كتب اتضافت مرتين بالغلط."""
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
    # تليجرام بيحدد أقصى طول للرسالة (4096 حرف)، لو القائمة كبيرة قوي نقسمها
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أنا بوت مكتبة. لو انت المالك ابعتلي كتاب في الخاص مع اسمه في الكابشن.\n"
        "وأي حد في الجروب يتكلم عن كتاب من الكتب المتضافة (بأي صياغة)، "
        "هبعتهوله تلقائي لو لقيت تطابق كويس."
    )


# ---------- المطابقة الذكية في الجروب وفي الخاص (بدون كلمة تريجر ثابتة) ----------

async def notify_not_found(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    """بترد على الشخص إن الكتاب مش متوفر حاليًا، وتبلغ المالك بالطلب ده
    عشان يعرف الكتب المطلوبة اللي لسه مضافاهاش، ومعاها زرار "رد على
    الشخص ده" عشان المالك يقدر يرد عليه مباشرة من غير ما يعرف الشات
    بتاعه بنفسه. مبنبلغش المالك لو هو نفسه اللي بيدور (زي لما يجرب يبحث
    بنفسه)."""
    await update.message.reply_text("😔 الكتاب ده مش متوفر حاليًا.")

    user = update.effective_user
    if not OWNER_ID or user.id == OWNER_ID:
        return

    chat = update.effective_chat
    chat_label = "الخاص" if chat.type == ChatType.PRIVATE else (chat.title or "جروب")
    username = f"@{user.username}" if user.username else user.full_name

    # بنخزن مكان الرسالة الأصلية (الشات ورقم الرسالة) عشان لو المالك دوس
    # على زرار الرد، نقدر نبعت رده كـ reply على نفس الرسالة دي في نفس
    # الشات (سواء جروب أو خاص)، من غير ما يحتاج يعرف حاجة تانية.
    key = f"{chat.id}_{update.message.message_id}"
    context.bot_data.setdefault("pending_reply_targets", {})[key] = {
        "chat_id": chat.id,
        "message_id": update.message.message_id,
        "user_label": username,
    }
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ رد على الشخص ده", callback_data=f"replyto:{key}")],
    ])
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "📩 حد سأل عن كتاب مش موجود عندك:\n\n"
                f"من: {username}\n"
                f"مكان الرسالة: {chat_label}\n"
                f"النص: {query_text}"
            ),
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("فشل إرسال تنبيه للمالك عن كتاب مش متوفر")


async def reply_target_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بيتنفذ لما المالك يدوس زرار "رد على الشخص ده" تحت تنبيه كتاب مش
    متوفر. بيحط المالك في وضع "انتظار رد": أول رسالة نصية يبعتها في
    الخاص بعد كده تتبعت تلقائي للشخص اللي سأل (كـ reply على رسالته
    الأصلية)، بدل ما تتفحص كطلب كتاب عادي."""
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return

    _, _, key = query.data.partition(":")
    pending = context.bot_data.get("pending_reply_targets", {})
    target = pending.get(key)
    if not target:
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="⏳ الطلب ده قديم أو اتعمل فيه رد قبل كده.",
        )
        return

    context.bot_data.setdefault("awaiting_reply", {})[query.from_user.id] = target
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=f"✍️ تمام، ابعت دلوقتي رسالتك في الخاص هنا وهتتبعت لـ {target['user_label']}.",
    )


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
    username = f"@{user.username}" if user.username else user.full_name
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


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    chat_type = update.effective_chat.type
    if chat_type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE):
        return

    user = update.effective_user

    # لو المالك في وضع "انتظار رد" (دوس زرار "رد على الشخص ده" تحت تنبيه
    # كتاب مش متوفر)، أول رسالة نصية يبعتها في الخاص بعد كده تتبعت
    # تلقائي للشخص ده بدل ما تتفحص كطلب كتاب عادي.
    if chat_type == ChatType.PRIVATE and is_owner(user.id):
        awaiting = context.bot_data.get("awaiting_reply", {})
        target = awaiting.pop(user.id, None)
        if target:
            try:
                await context.bot.send_message(
                    chat_id=target["chat_id"],
                    text=message.text,
                    reply_to_message_id=target["message_id"],
                )
                await message.reply_text(f"✅ اتبعتت لـ {target['user_label']}.")
            except Exception:
                logger.exception("فشل إرسال رد المالك للشخص")
                await message.reply_text("❌ حصلت مشكلة وأنا بابعت الرد، جرب تاني.")
            return

    # لو حد كلم البوت في الخاص، بنسجله عشان نقدر نستخدم /broadcast بعدين
    if chat_type == ChatType.PRIVATE:
        db.remember_user(user.id)

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
    # مفيش داعي نكلم الـ AI خالص - بس نبلغ الشخص والمالك إن الكتاب مش موجود.
    if (query_grade is not None or query_subject is not None) and not candidate_books:
        await notify_not_found(update, context, text)
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

    # لو مفيش تطابق منطقي، نبلغ الشخص إن الكتاب مش متوفر ونبلغ المالك بالطلب.
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
    app.add_handler(CommandHandler("addbook", add_book_cmd))
    app.add_handler(CommandHandler("listbooks", list_books_cmd))
    app.add_handler(CommandHandler("delbook", delete_book_cmd))
    app.add_handler(CommandHandler("dbinfo", dbinfo_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("dupes", find_duplicates_cmd))
    app.add_handler(CallbackQueryHandler(duplicate_decision_cb, pattern="^dup_"))
    app.add_handler(CallbackQueryHandler(reply_target_cb, pattern="^replyto:"))
    app.add_error_handler(error_handler)

    # رفع ملف من المالك في الخاص = إضافة كتاب تلقائي (لو فيه كابشن)
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
