"""Русские витринные тексты для колонок AW–AZ.

Витрина «Кандидаты» читает готовый текст, а не расшифровывает коды формулами:
логика вердикта должна жить в одном месте, а не размазываться по таблице.
"""
from app.checks import (
    FLAG_MATH,
    FLAG_MOBILE,
    FLAG_OUTLIER,
    FLAG_PASTED,
    FLAG_SUSPICIOUS,
    FLAG_TAB_SWITCH,
)

CONDITION_NAMES = {
    "stop_1_oklad": "работа без оклада",
    "stop_2_komissiya": "комиссия после проекта",
    "stop_3_kasaniya": "120–150 касаний в неделю",
    "stop_4_vstrechi": "15 встреч в неделю",
    "stop_5_vedenie": "самостоятельное ведение сделки",
    "stop_6_crm": "CRM и разборы звонков",
}

FLAG_NAMES = {
    FLAG_PASTED: "вставленный текст",
    FLAG_SUSPICIOUS: "текст без единой правки",
    FLAG_TAB_SWITCH: "долгий уход со страницы перед ответом",
    FLAG_MATH: "расхождение в цифрах по вопросу 1",
    FLAG_OUTLIER: "нереалистичная конверсия по вопросу 4",
    FLAG_MOBILE: "заполнено с телефона",
}

DECISION_TEXT = {
    "A": "A — сильный кандидат, звать на интервью",
    "B": "B — спорный, смотреть ответы",
    "C": "C — отказ",
}

MANUAL_DECISION = "Проверить руками"


def conditions_ru(rejected_fields: list[str]) -> str:
    if not rejected_fields:
        return "Все условия приняты"
    names = [CONDITION_NAMES.get(f, f) for f in rejected_fields]
    return "Не принял: " + ", ".join(names)


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


def flags_ru(flags: list[str], extra_reasons: list[str] | None = None) -> str:
    """Флаги плюс причины вердикта и причина ручной проверки — одной строкой."""
    parts = [FLAG_NAMES.get(f, f) for f in flags]
    if extra_reasons:
        parts.extend(extra_reasons)
    if not parts:
        return "Замечаний нет"
    return "; ".join(parts)
