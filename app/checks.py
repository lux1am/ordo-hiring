"""Автоматические проверки без обращения к модели.

Здесь только чистые функции над уже готовыми числами. Числа кандидат
вводит сам в отдельные поля формы, разбирает их numbers.py — ни одно
правило отсюда не обращается к сети и не зависит от трактовки текста.

Тут же считается балл за Q1: он выводится из девяти чисел арифметически,
поэтому спорную анкету можно пересчитать руками и получить тот же балл.
"""
import logging

from app.config import (
    CONVERSION_OUTLIER_THRESHOLD,
    MATH_TOLERANCE,
    PASTE_PCT_THRESHOLD,
    Q1_CHECK_SPREAD_MAX,
    Q1_STABLE_MIN_DEALS,
    SPEED_CPM_THRESHOLD_DESKTOP,
    SPEED_CPM_THRESHOLD_MOBILE,
    SUSPICIOUS_MIN_LENGTH,
    TAB_AWAY_SECONDS_THRESHOLD,
)
from app.models import OPEN_FIELDS, Submission, Telemetry
from app.numbers import Q1Numbers, Q4Numbers

log = logging.getLogger(__name__)

# --- Флаги ----------------------------------------------------------------
FLAG_PASTED = "PASTED"
FLAG_SUSPICIOUS = "SUSPICIOUS"
FLAG_TAB_SWITCH = "TAB_SWITCH"
FLAG_MATH = "MATH"
FLAG_OUTLIER = "OUTLIER"
FLAG_MOBILE = "MOBILE"


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


def numeric_flags(q1: Q1Numbers, q4: Q4Numbers) -> list[str]:
    """Флаги по числам из формы. Флага «чисел нет» больше не бывает.

    Все двенадцать полей обязательные, так что пустой анкеты сюда не
    доходит: вместо флага о нехватке данных ноль за Q1 ставит q1_score.
    """
    flags: list[str] = []

    # Сверка: сделки × средний чек ≈ выручка, допуск 10%.
    for m in q1.filled():
        expected = m.deals * m.check
        if expected <= 0:
            continue
        if abs(expected - m.revenue) / expected > MATH_TOLERANCE:
            log.info(
                "MATH: месяц %s — %d × %d = %d, заявлено %d",
                m.month, m.deals, m.check, expected, m.revenue,
            )
            _add(flags, FLAG_MATH)

    # Конверсия выше 50% для холодного outbound — выброс.
    for conv in (q4.conv_touch, q4.conv_deal):
        if conv > CONVERSION_OUTLIER_THRESHOLD:
            _add(flags, FLAG_OUTLIER)

    return flags


def checks_comparable(months: list) -> bool:
    """Средние чеки лежат в одном диапазоне: максимум не больше 2× минимума.

    Разброс на порядок означает, что месяцы несопоставимы — одна крупная
    сделка не то же самое, что ровный поток, и балл за стабильность за неё
    не даётся.
    """
    checks = [m.check for m in months]
    if not checks or min(checks) <= 0:
        return False
    return max(checks) <= min(checks) * Q1_CHECK_SPREAD_MAX


def q1_score(q1: Q1Numbers, definition_ok: bool) -> int:
    """Балл за Q1 по числам. Модель на него не влияет.

    0 — везде нули: результата за три месяца нет.
    1 — цифры есть не за все месяцы.
    2 — заполнены все три месяца.
    3 — плюс стабильность: минимум 3 сделки в каждом месяце и сопоставимые
        средние чеки.

    Без внятного определения закрытой сделки балл упирается в 2: цифры,
    про которые неизвестно, что именно ими считали, не доказывают
    стабильного результата.
    """
    months = q1.months
    filled = q1.filled()

    if not filled:
        score = 0
    elif len(filled) < len(months):
        score = 1
    else:
        stable = (
            all(m.deals >= Q1_STABLE_MIN_DEALS for m in filled)
            and checks_comparable(filled)
        )
        score = 3 if stable else 2

    if not definition_ok:
        score = min(score, 2)

    log.info(
        "q1: балл %d (месяцев с результатом %d из %d, определение %s)",
        score, len(filled), len(months), "есть" if definition_ok else "нет",
    )
    return score


def _add(flags: list[str], flag: str) -> None:
    if flag not in flags:
        flags.append(flag)
