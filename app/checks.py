"""Автоматические проверки без обращения к модели.

Здесь только чистые функции над уже готовыми числами. Извлечение чисел из
свободного текста живёт в extractor.py — так эти правила остаются
детерминированными и тестируются без сети.
"""
import logging

from app.config import (
    CONVERSION_OUTLIER_THRESHOLD,
    MATH_TOLERANCE,
    PASTE_PCT_THRESHOLD,
    Q1_MIN_CONFIRMED_MONTHS,
    SPEED_CPM_THRESHOLD_DESKTOP,
    SPEED_CPM_THRESHOLD_MOBILE,
    SUSPICIOUS_MIN_LENGTH,
    TAB_AWAY_SECONDS_THRESHOLD,
)
from app.models import OPEN_FIELDS, ExtractorOutput, Submission, Telemetry

log = logging.getLogger(__name__)

# --- Флаги ----------------------------------------------------------------
FLAG_PASTED = "PASTED"
FLAG_SUSPICIOUS = "SUSPICIOUS"
FLAG_TAB_SWITCH = "TAB_SWITCH"
FLAG_MATH = "MATH"
FLAG_OUTLIER = "OUTLIER"
FLAG_NO_NUMBERS = "NO_NUMBERS"
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


def numeric_flags(ex: ExtractorOutput | None) -> list[str]:
    """Флаги по числам из Q1 и Q4. None — экстрактор не отработал."""
    if ex is None or not ex.has_any_numbers():
        return [FLAG_NO_NUMBERS]

    flags: list[str] = []

    # Сверка: сделки × средний чек ≈ выручка, допуск 10%.
    for m in ex.confirmed_months():
        expected = m.deals * m.avg_check
        if expected <= 0:
            continue
        if abs(expected - m.revenue) / expected > MATH_TOLERANCE:
            log.info(
                "MATH: месяц %s — %.0f × %.0f = %.0f, заявлено %.0f",
                m.month, m.deals, m.avg_check, expected, m.revenue,
            )
            _add(flags, FLAG_MATH)

    # Конверсия выше 50% для холодного outbound — выброс.
    for conv in (ex.conv_touch_meeting, ex.conv_meeting_deal):
        if conv is not None and conv > CONVERSION_OUTLIER_THRESHOLD:
            _add(flags, FLAG_OUTLIER)

    return flags


def q1_needs_manual(ex: ExtractorOutput | None) -> bool:
    """Меньше двух месяцев с полной тройкой чисел — смотреть руками.

    Полная тройка = сделки, выручка и средний чек одновременно.
    """
    if ex is None:
        return True
    return len(ex.confirmed_months()) < Q1_MIN_CONFIRMED_MONTHS


def _add(flags: list[str], flag: str) -> None:
    if flag not in flags:
        flags.append(flag)
