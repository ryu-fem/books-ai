"""
مطابقة ذكية باستخدام Groq API - مجاني بالكامل (مفيش أي فلوس مطلوبة، بس فيه
حد أقصى لعدد الطلبات في الدقيقة/اليوم وده أكتر من كافي لبوت عادي).
الموديل بيفهم قصد المستخدم بالمعنى، بما في ذلك الاختصارات وأجزاء العنوان
والعامية، مش بس تطابق حرفي.
"""

import os
import re

import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL_NAME = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")


class AIMatchUnavailable(Exception):
    """بترفع لما الـ AI مش قادر يستجيب أصلاً (مفتاح مفقود، مشكلة نت، حد
    الطلبات اتعدى..) - مختلف عن إن الـ AI رد بس قال مفيش تطابق."""


def ai_pick_book(query: str, books: list):
    """
    books: list of tuples (id, title, file_id, file_name)
    بيرجع book_id لو الموديل لقى تطابق منطقي، أو None لو الموديل شغال ورد
    لكن مالقاش أي كتاب يطابق (ده رد شرعي، مش خطأ). بيرمي AIMatchUnavailable
    لو الموديل نفسه فشل يستجيب أصلاً.
    """
    if not books or not query.strip():
        return None

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise AIMatchUnavailable("GROQ_API_KEY مش متظبط")

    listing = "\n".join(f"{b[0]}. {b[1]}" for b in books)
    prompt = (
        "انت مساعد بيحدد أي كتاب من قائمة كتب متاحة يقصده المستخدم. "
        "لازم تفهم حتى لو المستخدم:\n"
        "- كتب اختصار للعنوان (مثلاً \"مقدمة ابن خلدون\" بدل الاسم الكامل)\n"
        "- كتب جزء بس من الاسم\n"
        "- وصف موضوع الكتاب بمعناه من غير ما يقول اسمه بالظبط\n"
        "- غلط إملائيًا أو كتب بالعامية المصرية\n\n"
        f'رسالة المستخدم: "{query}"\n\n'
        f"قائمة الكتب المتاحة (رقم. العنوان):\n{listing}\n\n"
        "رد برقم الكتاب المطابق فقط، رقم واحد بدون أي كلام إضافي. لو مفيش "
        "أي كتاب في القائمة يطابق قصد المستخدم بشكل معقول، رد بالرقم 0 بالظبط."
    )

    try:
        response = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 10,
                "temperature": 0,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        # أي مشكلة (نت، مفتاح غلط، تعدي حد الطلبات..) = الموديل مش متاح
        raise AIMatchUnavailable(str(exc)) from exc

    match = re.search(r"\d+", text)
    if not match:
        return None
    book_id = int(match.group())
    valid_ids = {b[0] for b in books}
    return book_id if book_id in valid_ids else None
