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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==== الإعدادات ====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ضع_التوكن_هنا")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))  # ايدي التليجرام بتاعك

# الجمل اللي لو ظهرت في رسالة حد في الجروب، البوت هيدور على اسم كتاب بعدها
TRIGGER_PHRASES = [
    "عايز",
    "عاوز",
    "عايزة",
    "محتاج",
    "ابحث عن",
    "دور على",
    "ممكن كتاب",
    "فين كتاب",
    "عندك كتاب",
]

MATCH_THRESHOLD = 62  # نسبة التشابه المطلوبة عشان يبعت الكتاب (من 0 لـ100)


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
    text = "\n".join(f"{b[0]}. {b[1]}" for b in books)
    await update.message.reply_text(f"📚 قائمة الكتب:\n{text}")


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


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أنا بوت مكتبة. لو انت المالك ابعتلي كتاب في الخاص مع اسمه في الكابشن.\n"
        "وأي حد في الجروب يقول 'عايز كذا' وهدوّر على أقرب كتاب واببعتهوله تلقائي."
    )


# ---------- المطابقة الذكية في الجروب ----------

def extract_query(text: str):
    """يدور على جملة الطلب ويطلع اسم الكتاب المحتمل بعدها."""
    lowered = text.strip()
    for phrase in TRIGGER_PHRASES:
        idx = lowered.find(phrase)
        if idx != -1:
            after = lowered[idx + len(phrase):].strip(" :.,؟!")
            if after:
                return after
    return None


async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    query = extract_query(message.text)
    if not query:
        return

    books = db.get_all_books()
    if not books:
        return

    titles = [b[1] for b in books]
    result = process.extractOne(query, titles, scorer=fuzz.WRatio)
    if not result:
        return

    matched_title, score, idx = result
    if score < MATCH_THRESHOLD:
        return

    book = books[idx]
    book_id, title, file_id, file_name = book
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=file_id,
        caption=f"📖 {title}",
        reply_to_message_id=message.message_id,
    )


def main():
    if BOT_TOKEN == "ضع_التوكن_هنا" or OWNER_ID == 0:
        print("⚠️ لازم تحط BOT_TOKEN و OWNER_ID كمتغيرات بيئة قبل التشغيل.")

    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addbook", add_book_cmd))
    app.add_handler(CommandHandler("listbooks", list_books_cmd))
    app.add_handler(CommandHandler("delbook", delete_book_cmd))

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
