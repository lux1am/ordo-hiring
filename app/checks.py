"""Автоматические проверки без обращения к модели.

Здесь только чистые функции над уже разобранными ответами: ни одно правило
отсюда не обращается к сети и не зависит от трактовки текста.

Две группы правил. Первая — стоп-вопросы: у каждого задан проходной набор
вариантов, и любой непроходной ответ означает отказ ещё до вызова модели.
Вторая — вопросы-флаги: баллов они не дают, но часть из них понижает
вердикт A до B, см. app/verdict.py.
"""
import logging

from app.config import (
    PASTE_PCT_THRESHOLD,
    SPEED_CPM_THRESHOLD_DESKTOP,
    SPEED_CPM_THRESHOLD_MOBILE,
    SUSPICIOUS_MIN_LENGTH,
    TAB_AWAY_SECONDS_THRESHOLD,
    load_questions,
)
from app.models import OPEN_FIELDS, Submission, Telemetry
from app.profile import stop_option

log = logging.getLogger(__name__)

# --- Флаги телеметрии -----------------------------------------------------
FLAG_PASTED = "PASTED"
FLAG_SUSPICIOUS = "SUSPICIOUS"
FLAG_TAB_SWITCH = "TAB_SWITCH"
FLAG_MOBILE = "MOBILE"

# --- Флаги по вопросам-флагам ---------------------------------------------
FLAG_MOTIVATION_AVOIDANCE = "MOTIVATION_AVOIDANCE"
FLAG_INCOME_LOW = "INCOME_LOW"
FLAG_INCOME_UNREAL = "INCOME_UNREAL"
FLAG_STABILITY_FIRST = "STABILITY_FIRST"
FLAG_INCOME_NOT_PRIORITY = "INCOME_NOT_PRIORITY"
FLAG_UNDERESTIMATE = "UNDERESTIMATE"
FLAG_PESSIMIST = "PESSIMIST"
FLAG_NO_GOALS = "NO_GOALS"


def telemetry_flags(sub: Submission) -> list[str]:
    """Флаги по поведению в форме. Мобильные проверяются мягче."""
    t: Telemetry = sub.telemetry
    flags: list[str] = []

    if t.is_mobile:
        flags.append(FLAG_MOBILE)

    speed_limit = (
        SPEED_CPM_THRESHOLD_MOBILE if t.is_mobile else SPEED_CPM_THRESHOLD_DESKTOP
    )

    for field in OPEN_FIELDS:
        m = t.field(field)

        # Вставка: событие paste надёжно работает и на мобильных.
        if m.paste_pct > PASTE_PCT_THRESHOLD:
            _add(flags, FLAG_PASTED)
        # Скорость набора: свайп-ввод и автозамена на телефоне дают
        # закономерно высокие цифры, поэтому порог там выше.
        elif m.speed_cpm > speed_limit:
            _add(flags, FLAG_PASTED)

        # Текст без единой правки. На мобильных keydown по Backspace
        # приходит нестабильно, поэтому правило там отключено целиком.
        if (
            not t.is_mobile
            and m.backspaces == 0
            and m.length > SUSPICIOUS_MIN_LENGTH
        ):
            _add(flags, FLAG_SUSPICIOUS)

        # Долгий уход со вкладки, после которого кандидат дописывал ответ.
        if m.away_before_input_sec > TAB_AWAY_SECONDS_THRESHOLD:
            _add(flags, FLAG_TAB_SWITCH)

    return flags


# ============================ Стоп-вопросы ================================
#
# Ключи — имена колонок листа, они же имена полей формы. Текст каждого
# вопроса живёт в questions.json, здесь задан только проходной набор
# вариантов: всё, что вне набора, — отказ без вызова модели.

STOP_PASS: dict[str, tuple[str, ...]] = {
    "stop_1_sdelno": ("Да, полностью готов", "Скорее да, понимаю риски"),
    # Десяти-четырнадцати часов в неделю мало.
    "stop_2_chasy": ("15–19", "20–24", "25+"),
    "stop_3_svyaz": ("Да",),
    "stop_4_oformlenie": ("ГПХ", "Самозанятость"),
    # Рабочее место: частичной готовности недостаточно.
    "stop_5_tehnika": ("Да, всё есть",),
    "stop_6_zapis": ("Да, полностью готов", "Скорее да"),
}


def stop_failures(stops: dict[str, str]) -> list[str]:
    """Поля стоп-вопросов с непроходным ответом.

    Пустой ответ тоже непроходной: до сюда доходит уже записанная строка, и
    пустая ячейка означает не «вопрос не задавали», а «ответа нет».
    """
    return [
        field
        for field, allowed in STOP_PASS.items()
        if stop_option(stops.get(field, "")) not in allowed
    ]


# ========================== Вопросы-флаги =================================
#
# Варианты названы строками, а не номерами: номер молча переехал бы вместе
# с порядком в questions.json. Совпадение проверяется при импорте — см.
# _assert_known ниже.

MOTIVE_AVOIDANCE = ("Уйти на удалёнку", "Сменить сферу")
INCOME_LOW = "до 50 000 ₽"
INCOME_UNREAL = "более 150 000 ₽"
MEETINGS_UNDERESTIMATE = "Легко достижимо"
MEETINGS_PESSIMIST = "Нереально"
GOALS_UNKNOWN = "Не знаю"

RANK_INCOME = "flag_rank_income"
RANK_STABILITY = "flag_rank_stability"
# Доход, отодвинутый на третье или четвёртое место, — не приоритет.
INCOME_LOW_PLACES = ("3", "4")


def profile_flags(profile: dict) -> list[str]:
    """Флаги по ответам на вопросы-флаги. Баллов они не меняют."""
    flags: list[str] = []

    # Мотивация «лишь бы уйти»: единственный выбор — удалёнка или смена
    # сферы. Вместе с любым другим вариантом это уже не бегство.
    motive = list(profile.get("flag_motive") or [])
    if len(motive) == 1 and motive[0] in MOTIVE_AVOIDANCE:
        _add(flags, FLAG_MOTIVATION_AVOIDANCE)

    income = profile.get("flag_income") or ""
    if income == INCOME_LOW:
        _add(flags, FLAG_INCOME_LOW)
    elif income == INCOME_UNREAL:
        _add(flags, FLAG_INCOME_UNREAL)

    rank = profile.get("flag_rank") or {}
    if rank.get(RANK_STABILITY) == "1":
        _add(flags, FLAG_STABILITY_FIRST)
    if rank.get(RANK_INCOME) in INCOME_LOW_PLACES:
        _add(flags, FLAG_INCOME_NOT_PRIORITY)

    meetings = profile.get("flag_meetings") or ""
    if meetings == MEETINGS_UNDERESTIMATE:
        _add(flags, FLAG_UNDERESTIMATE)
    elif meetings == MEETINGS_PESSIMIST:
        _add(flags, FLAG_PESSIMIST)

    if (profile.get("flag_goals") or "") == GOALS_UNKNOWN:
        _add(flags, FLAG_NO_GOALS)

    return flags


def _add(flags: list[str], flag: str) -> None:
    if flag not in flags:
        flags.append(flag)


# --- Сверка с questions.json ----------------------------------------------
#
# Правила выше названы теми же строками, что варианты в анкете. Опечатка в
# коротком тире или лишний пробел не вызвали бы ошибки — они молча сделали
# бы правило нерабочим: проходным не оказался бы ни один ответ. Поэтому
# совпадение проверяется при импорте модуля, а не на боевой анкете.

def _options(field: str) -> set[str]:
    questions = load_questions()
    for q in questions["stop_questions"] + questions["flag_questions"]:
        if q["field"] == field:
            if q.get("type") == "rank":
                return set(q["places"])
            return set(q["options"])
    raise AssertionError(f"в questions.json нет вопроса {field}")


def _assert_known(field: str, values) -> None:
    unknown = sorted(set(values) - _options(field))
    if unknown:
        raise AssertionError(
            f"в questions.json у вопроса {field} нет вариантов: {', '.join(unknown)}"
        )


for _field, _allowed in STOP_PASS.items():
    _assert_known(_field, _allowed)

_assert_known("flag_motive", MOTIVE_AVOIDANCE)
_assert_known("flag_income", (INCOME_LOW, INCOME_UNREAL))
_assert_known("flag_meetings", (MEETINGS_UNDERESTIMATE, MEETINGS_PESSIMIST))
_assert_known("flag_goals", (GOALS_UNKNOWN,))
_assert_known("flag_rank", INCOME_LOW_PLACES)

assert set(STOP_PASS) == {q["field"] for q in load_questions()["stop_questions"]}, (
    "проходной набор задан не для всех стоп-вопросов"
)
