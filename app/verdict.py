"""Итоговый вердикт: сумма баллов, пороги и понижения."""
import logging
from dataclasses import dataclass, field

from app.checks import (
    FLAG_INCOME_UNREAL,
    FLAG_MOTIVATION_AVOIDANCE,
    FLAG_PASTED,
    FLAG_STABILITY_FIRST,
    FLAG_SUSPICIOUS,
    FLAG_TAB_SWITCH,
)
from app.config import AI_STYLE_DOWNGRADE_THRESHOLD, VERDICT_A_MIN, VERDICT_B_MIN
from app.models import QKEYS

log = logging.getLogger(__name__)

A, B, C = "A", "B", "C"

# Флаги о том, КАК анкета заполнялась, а не о том, ЧТО в ней написано.
# Вставка, отсутствие правок и уход со страницы не доказывают ни слабости
# кандидата, ни обмана — на балл они не влияют и вердикт не понижают.
# Их место — вопрос на интервью: проверить те же ответы голосом.
TELEMETRY_FLAGS = (FLAG_PASTED, FLAG_SUSPICIOUS, FLAG_TAB_SWITCH)

TELEMETRY_SCREENING_RU = "уточнить ответы голосом — часть текста вставлена"

# Флаги вопросов о мотивации, которые понижают A до B. Остальные флаги той
# же группы (заниженный доход, недооценка нагрузки, отсутствие цели) только
# показываются рекрутёру: сами по себе они не отменяют сильных ответов.
DOWNGRADE_FLAGS = {
    FLAG_STABILITY_FIRST: "стабильность на первом месте",
    FLAG_INCOME_UNREAL: "нереалистичные ожидания по доходу",
    FLAG_MOTIVATION_AVOIDANCE: "мотивация — уйти, а не продавать",
}


def screening_additions(flags: list[str]) -> list[str]:
    """Дополнительные вопросы на интервью, продиктованные флагами.

    В flags_ru телеметрия пишется по-прежнему — она видна в таблице; здесь
    же она превращается в действие вместо понижения вердикта.
    """
    if any(f in flags for f in TELEMETRY_FLAGS):
        return [TELEMETRY_SCREENING_RU]
    return []


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


def decide(scores: dict[str, int], flags: list[str], ai_style: int) -> VerdictResult:
    """Полный расчёт вердикта. Порядок правил важен.

    1. Сумма баллов даёт базовый вердикт по порогам: 12–15 → A, 9–11 → B,
       0–8 → C.
    2. A понижается до B при флагах мотивации из DOWNGRADE_FLAGS или при
       высоком стиле ИИ. Телеметрия (вставка, отсутствие правок, уход со
       страницы) вердикт не трогает — она уходит вопросом на интервью,
       см. screening_additions.

    Отказ по стоп-вопросам сюда не доходит: он ставится до вызова модели,
    см. app/pipeline.py.
    """
    total = sum(int(scores.get(k, 0)) for k in QKEYS)
    verdict = by_score(total)
    reasons: list[str] = []

    if verdict == A:
        downgrades = [DOWNGRADE_FLAGS[f] for f in DOWNGRADE_FLAGS if f in flags]
        if ai_style > AI_STYLE_DOWNGRADE_THRESHOLD:
            downgrades.append(f"стиль ИИ {ai_style}/100")
        if downgrades:
            verdict = B
            reasons.append("A понижен до B: " + ", ".join(downgrades))

    log.info("вердикт: %s (сумма %d, флаги: %s)", verdict, total, ",".join(flags) or "нет")
    return VerdictResult(verdict=verdict, total=total, reasons=reasons)
