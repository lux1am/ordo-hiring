"""Русские витринные тексты для колонок AW–AZ.

Витрина «Кандидаты» читает готовый текст, а не расшифровывает коды формулами:
логика вердикта должна жить в одном месте, а не размазываться по таблице.
"""
import re

from app.config import load_questions
from app.checks import (
    FLAG_INCOME_LOW,
    FLAG_INCOME_NOT_PRIORITY,
    FLAG_INCOME_UNREAL,
    FLAG_MOBILE,
    FLAG_MOTIVATION_AVOIDANCE,
    FLAG_NO_GOALS,
    FLAG_PASTED,
    FLAG_PESSIMIST,
    FLAG_STABILITY_FIRST,
    FLAG_SUSPICIOUS,
    FLAG_TAB_SWITCH,
    FLAG_UNDERESTIMATE,
)

# Короткие названия условий для витрины: в conditions_ru они перечисляются
# через запятую, поэтому полные формулировки из questions.json не годятся.
CONDITION_NAMES = {
    "stop_1_sdelno": "сдельный доход без оклада",
    "stop_2_chasy": "15 и больше часов в неделю",
    "stop_3_svyaz": "связь в будни с 11:00 до 17:00 по МСК",
    "stop_4_oformlenie": "оформление по ГПХ или самозанятости",
    "stop_5_tehnika": "компьютер, гарнитура и стабильный интернет",
    "stop_6_zapis": "запись звонков и разборы на планёрках",
}

FLAG_NAMES = {
    FLAG_PASTED: "вставленный текст",
    FLAG_SUSPICIOUS: "текст без единой правки",
    FLAG_TAB_SWITCH: "долгий уход со страницы перед ответом",
    FLAG_MOBILE: "заполнено с телефона",
    FLAG_MOTIVATION_AVOIDANCE: "мотивация — уйти, а не продавать",
    FLAG_INCOME_LOW: "заниженные ожидания по доходу",
    FLAG_INCOME_UNREAL: "нереалистичные ожидания по доходу",
    FLAG_STABILITY_FIRST: "стабильность важнее дохода",
    FLAG_INCOME_NOT_PRIORITY: "доход не в приоритете",
    FLAG_UNDERESTIMATE: "недооценивает нагрузку по встречам",
    FLAG_PESSIMIST: "считает план по встречам недостижимым",
    FLAG_NO_GOALS: "нет цели на ближайший год",
}

DECISION_TEXT = {
    "A": "A — сильный кандидат, звать на интервью",
    "B": "B — спорный, смотреть ответы",
    "C": "C — отказ",
}

MANUAL_DECISION = "Проверить руками"

OTHER_OPTION = next(
    (
        q["other_option"]
        for q in load_questions()["flag_questions"]
        if q.get("other_option")
    ),
    "",
)


# По этой приставке отказ по стоп-вопросам отличают от всего остального —
# и формулы листа «Эффективность», и еженедельный отчёт. Поэтому она
# константа: перепиши её в тексте, и обе сводки молча покажут нули.
REJECTED_PREFIX = "Не проходит: "
ACCEPTED_TEXT = "Все условия приняты"


def conditions_ru(rejected_fields: list[str]) -> str:
    if not rejected_fields:
        return ACCEPTED_TEXT
    names = [CONDITION_NAMES.get(f, f) for f in rejected_fields]
    return REJECTED_PREFIX + ", ".join(names)


def decision_ru(verdict: str, is_manual: bool = False) -> str:
    if is_manual:
        return MANUAL_DECISION
    return DECISION_TEXT.get(verdict, verdict)


def ai_ru(ai_style: int | None, markers: list[str] | None) -> str:
    if ai_style is None:
        return "Стиль ИИ не оценивался"
    if ai_style > 70:
        level = "высокий"
    elif ai_style >= 30:
        level = "средний"
    else:
        level = "низкий"
    text = f"Стиль ИИ: {ai_style}/100 — {level}"
    if markers:
        text += ". Признаки: " + ", ".join(markers)
    return text


# --- Ответы о мотивации одной строкой -------------------------------------
#
# Сами ответы лежат JSON-ом в колонке ai_markers — читать его глазами в
# таблице неудобно, поэтому в витрину идёт та же информация фразой.

NO_REMARKS = "Замечаний нет"

MONEY_RANGE_RE = re.compile(r"(\d+) 000\s*–\s*(\d+) 000")
MONEY_ONE_RE = re.compile(r"(\d+) 000")


def short_money(text: str) -> str:
    """«80 000–120 000 ₽» → «80–120к». Незнакомый формат остаётся как есть."""
    out = MONEY_RANGE_RE.sub(r"\1–\2к", text)
    if out == text:
        out = MONEY_ONE_RE.sub(r"\1к", text)
    return out.replace(" ₽", "").strip()


def _decap(text: str) -> str:
    """Первая буква строчной — но не у аббревиатур вроде «РОП» и «ГПХ»."""
    head = text.split(" ", 1)[0]
    if head.isupper():
        return text
    return text[:1].lower() + text[1:]


def _rank_order(rank: dict) -> str:
    """Строки ранжирования по местам, от первого к четвёртому."""
    rows = {}
    for q in load_questions()["flag_questions"]:
        if q.get("type") == "rank":
            rows = {r["field"]: r.get("short") or r["label"] for r in q["rows"]}
            break
    named = [(place, rows[f]) for f, place in rank.items() if f in rows]
    return ", ".join(name for _, name in sorted(named))


def profile_ru(profile: dict | None) -> str:
    """Ответы на вопросы о мотивации фразой для витрины.

    «мотивация: высокий доход без потолка; доход 80–120к; приоритет: доход,
    график, рост, стабильность; встречи — достижимо при усилиях; цель — РОП»
    """
    if not profile:
        return ""

    parts = []

    motive = list(profile.get("flag_motive") or [])
    other = str(profile.get("flag_motive_other") or "").strip()
    if other:
        # Свой вариант показывается текстом кандидата, а не словом «Другое».
        motive = [other if m == OTHER_OPTION else m for m in motive]
    if motive:
        parts.append("мотивация: " + ", ".join(_decap(m) for m in motive))

    income = str(profile.get("flag_income") or "").strip()
    if income:
        parts.append("доход " + short_money(income))

    order = _rank_order(profile.get("flag_rank") or {})
    if order:
        parts.append("приоритет: " + order)

    meetings = str(profile.get("flag_meetings") or "").strip()
    if meetings:
        parts.append("встречи — " + _decap(meetings))

    goals = str(profile.get("flag_goals") or "").strip()
    if goals:
        parts.append("цель — " + _decap(goals))

    return "; ".join(parts)


def flags_ru(
    flags: list[str],
    extra_reasons: list[str] | None = None,
    profile: dict | None = None,
) -> str:
    """Флаги, причины вердикта и ответы о мотивации — одной строкой."""
    parts = [FLAG_NAMES.get(f, f) for f in flags]
    if extra_reasons:
        parts.extend(extra_reasons)
    if not parts:
        parts = [NO_REMARKS]

    answers = profile_ru(profile)
    if answers:
        parts.append(answers)
    return "; ".join(parts)
