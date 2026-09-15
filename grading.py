def detect_grade(title: str) -> str:
    """بيحدد الصف الدراسي من نص العنوان نفسه، عشان نقدر نعرض الكتب مجمّعة
    ونوضح للـ AI مرحلة كل كتاب، من غير ما نحتاج المالك يضيف تصنيف يدوي."""
    return _detect_grade_from_text(title)


def detect_query_grade(text: str):
    """بيحاول يحدد المرحلة المطلوبة من نص رسالة المستخدم نفسها. بيرجع
    None لو الرسالة معملتش تحديد واضح للمرحلة."""
    grade = _detect_grade_from_text(text)
    return None if grade == "غير مصنّف" else grade


def _detect_grade_from_text(text: str) -> str:
    t = text.replace("ة", "ه")  # توحيد التاء المربوطة/الهاء عشان المطابقة تبقى أسهل

    if "تالت" in t and "ثانو" in t:
        return "تالتة ثانوي"
    if "اول" in t and "ثانو" in t:
        return "أولى ثانوي"
    if "تاني" in t and "ثانو" in t:
        if "مش بكالوريا" in t or "مش بكالوريه" in t:
            return "تانية ثانوي (مش بكالوريا)"
        if "بكالوريا" in t or "بكالوريه" in t:
            return "تانية ثانوي (بكالوريا)"
        return "تانية ثانوي"
    return "غير مصنّف"


def grade_matches(book_grade: str, query_grade) -> bool:
    """بيتأكد إن مرحلة الكتاب بتتطابق مع المرحلة المطلوبة في رسالة
    المستخدم. لو المستخدم قال "تانية ثانوي" من غير تحديد بكالوريا/مش
    بكالوريا، بيتقبل أي كتاب من مرحلة "تانية ثانوي" بغض النظر عن التفصيلة
    دي."""
    if query_grade is None:
        return True
    if book_grade == query_grade:
        return True
    if query_grade == "تانية ثانوي" and book_grade.startswith("تانية ثانوي"):
        return True
    return False
