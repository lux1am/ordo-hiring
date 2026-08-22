"""Итоговый вердикт: сумма баллов, пороги, понижения и автофильтры."""
import logging
from dataclasses import dataclass, field

from app.checks import FLAG_MATH, FLAG_PASTED
from app.config import AI_STYLE_DOWNGRADE_THRESHOLD, VERDICT_A_MIN, VERDICT_B_MIN
from app.models import QKEYS

log = logging.getLogger(__name__)

A, B, C = "A", "B", "C"


@dataclass
class VerdictResult:
    verdict: str
    total: int
    reasons: list[str] = field(default_factory=list)   # человекочитаемо, по-русски


def by_score(total: int) -> str:
    if total >= VERDICT_A_MIN:
        return A
    if total >= VERDICT_B_MIN:
        return B
    return C


def decide(
    scores: dict[str, int],
    flags: list[str],
    ai_style: int,
    q3_senior_dm_confirmed: bool,
) -> VerdictResult:
    """Полный расчёт вердикта. Порядок правил важен.

    1. Сумма баллов даёт базовый вердикт по порогам.
    2. A понижается до B при вставке, расхождении в цифрах или высоком стиле ИИ.
    3. Автофильтр по Q3 ставит C независимо от суммы — он поверх всего.
    """
    total = sum(int(scores.get(k, 0)) for k in QKEYS)
    verdict = by_score(total)
    reasons: list[str] = []

    if verdict == A:
        downgrades = []
        if FLAG_PASTED in flags:
            downgrades.append("вставленный текст")
        if FLAG_MATH in flags:
            downgrades.append("расхождение в цифрах")
        if ai_style > AI_STYLE_DOWNGRADE_THRESHOLD:
            downgrades.append(f"стиль ИИ {ai_style}/100")
        if downgrades:
            verdict = B
            reasons.append("A понижен до B: " + ", ".join(downgrades))

    # Автофильтр по Q3 — последним, он перекрывает и сумму, и понижение.
    if not q3_senior_dm_confirmed:
        if verdict != C:
            reasons.append(
                "C по автофильтру: нет подтверждённых кейсов с собственниками, "
                "CTO или ИТ-директорами"
            )
        verdict = C

    log.info("вердикт: %s (сумма %d, флаги: %s)", verdict, total, ",".join(flags) or "нет")
    return VerdictResult(verdict=verdict, total=total, reasons=reasons)
