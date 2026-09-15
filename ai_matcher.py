"""
مطابقة ذكية باستخدام Groq API - مجاني بالكامل. الشرط الأساسي: لازم يتحقق
مع بعض (المرحلة الدراسية + المادة) عشان أي كتاب يتحسب مطابق، ولو اتحققوا
بيرجع كل الكتب المطابقة (كل الأجزاء) مش كتاب واحد بس.
"""

import os
import re

import requests

from grading import detect_grade

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.3-70b-versatile موديل أكبر وأدق في التمييز بين عناوين متقاربة
# (لسه مجاني بالكامل على Groq، بس أبطأ شوية من الموديل الأصغر)
MODEL_NAME = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


class AIMatchUnavailable(Exception):
    """بترفع لما الـ AI مش قادر يستجيب أصلاً (مفتاح مفقود، مشكلة نت، حد
    الطلبات اتعدى..) - مختلف عن إن الـ AI رد بس قال مفيش تطابق."""


def ai_pick_books(query: str, books: list):
    """
    books: list of tuples (id, title, file_id, file_name)
    بيرجع list فيها كل الـ book_id اللي فعلاً مطابقين لطلب المستخدم، بشرط
    إن الاتنين دول يتحققوا مع بعض:
      1) المرحلة/السنة الدراسية
      2) المادة
    لو الرسالة مذكرتش مرحلة أو مادة واضحة، أو مفيش كتب تطابق الشرطين مع
    بعض، بترجع [] (قائمة فاضية - ده رد شرعي، مش خطأ).
    بترمي AIMatchUnavailable لو الـ API نفسه فشل يستجيب أصلاً.
    """
    if not books or not query.strip():
        return []

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise AIMatchUnavailable("GROQ_API_KEY مش متظبط")

    listing = "\n".join(f"{b[0]}. [{detect_grade(b[1])}] {b[1]}" for b in books)
    prompt = (
        "انت مساعد بيحدد كل الكتب اللي تطابق طلب المستخدم من قائمة كتب "
        "مدرسية متاحة. كل كتاب قدامه تصنيف المرحلة الدراسية بين قوسين "
        "معمول بشكل آلي، ممكن يكون \"غير مصنّف\" لو مقدرش يحدد.\n\n"
        "شرط أساسي وصارم: الكتاب يتحسب مطابق للطلب لو وبس لو اتحقق الشرطين "
        "دول مع بعض:\n"
        "1) المرحلة/السنة الدراسية اللي المستخدم طلبها (أو فهمت ضمنيًا من "
        "رسالته)\n"
        "2) المادة الدراسية اللي المستخدم طلبها\n\n"
        "لو الرسالة مفيهاش تحديد واضح (ولو ضمني) للمرحلة، أو مفيهاش تحديد "
        "واضح للمادة، متختارش أي كتاب خالص - رد 0.\n\n"
        "لو الشرطين اتحققوا، رجّع كل الكتب اللي مادتهم ومرحلتهم متطابقين "
        "(كل الأجزاء/الفصول المتاحة لنفس المادة ونفس المرحلة)، مش كتاب "
        "واحد بس.\n\n"
        "خد بالك إن عناوين الكتب ممكن يكون فيها أخطاء إملائية بسيطة (زي "
        "\"ثاموي\" بدل \"ثانوي\")، ده ميمنعكش من المطابقة الصحيحة.\n\n"
        f'رسالة المستخدم: "{query}"\n\n'
        f"قائمة الكتب المتاحة (رقم. [المرحلة] العنوان):\n{listing}\n\n"
        "رد بأرقام كل الكتب المطابقة مفصولة بفاصلة بس، من غير أي كلام "
        "إضافي (مثال: 8,9). لو مفيش أي كتاب يستحق المطابقة حسب القواعد "
        "فوق، رد بالرقم 0 بالظبط ولا حاجة تانية."
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
                "max_tokens": 60,
                "temperature": 0,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        # أي مشكلة (نت، مفتاح غلط، تعدي حد الطلبات..) = الموديل مش متاح
        raise AIMatchUnavailable(str(exc)) from exc

    valid_ids = {b[0] for b in books}
    found_ids = [int(n) for n in re.findall(r"\d+", text)]
    # 0 مش رقم كتاب حقيقي فهيتفلتر تلقائي هنا
    result_ids = [n for n in found_ids if n in valid_ids]

    print(f"[ai_matcher] query={query!r} raw_response={text!r} -> book_ids={result_ids}")
    return result_ids
