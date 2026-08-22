"""Извлечение чисел из свободного текста ответов на Q1 и Q4.

Регулярками месячные тройки из живого текста не вытащить, поэтому здесь
дешёвый вызов модели, который ничего не считает и не оценивает — только
находит числа. Всю арифметику дальше делает checks.py.
"""
import json
import logging

from pydantic import ValidationError

from app.config import EXTRACTOR_MAX_TOKENS, EXTRACTOR_MODEL, load_extractor_prompt
from app.llm import client, parse_json, text_of
from app.models import ExtractorOutput
from app.retry import RetriesExhausted, call_with_backoff

log = logging.getLogger(__name__)


def build_prompt(q1_text: str, q4_text: str) -> str:
    return (
        "=== Ответ на вопрос 1 (сделки за последние 3 месяца) ===\n"
        f"{q1_text}\n\n"
        "=== Ответ на вопрос 4 (показатели воронки) ===\n"
        f"{q4_text}"
    )


def extract(q1_text: str, q4_text: str) -> ExtractorOutput | None:
    """Возвращает числа либо None, если извлечь не удалось.

    None — это не ошибка обработки: вызывающий ставит флаг NO_NUMBERS и
    продолжает. Анкета из-за неудачного извлечения не теряется.
    """
    def call():
        return client().messages.create(
            model=EXTRACTOR_MODEL,
            max_tokens=EXTRACTOR_MAX_TOKENS,
            temperature=0,
            system=load_extractor_prompt(),
            messages=[{"role": "user", "content": build_prompt(q1_text, q4_text)}],
        )

    try:
        raw = text_of(call_with_backoff(call, label="экстрактор"))
    except RetriesExhausted as e:
        log.error("экстрактор недоступен: %s", e)
        return None
    except Exception:
        log.exception("экстрактор: неожиданная ошибка")
        return None

    try:
        return ExtractorOutput.model_validate(parse_json(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        log.warning("экстрактор вернул неразбираемый ответ: %s | сырое: %.300s", e, raw)
        return None
