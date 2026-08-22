"""Общая обвязка над Anthropic API: клиент, извлечение JSON, нормализация."""
import json
import logging
import re
from functools import lru_cache

import anthropic

from app.config import ANTHROPIC_API_KEY

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def client() -> anthropic.Anthropic:
    """Клиент с отключёнными ретраями SDK.

    Бэкофф свой (app/retry.py). Если оставить встроенные ретраи, задержки
    перемножатся: три своих попытки × три SDK-шных дадут девять запросов
    и минуты ожидания на каждой анкете во время волны откликов.
    """
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=0, timeout=120.0)


def text_of(response) -> str:
    """Склейка текстовых блоков ответа."""
    return "".join(b.text for b in response.content if b.type == "text").strip()


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json(raw: str) -> dict:
    """Разбор JSON из ответа модели, устойчивый к markdown-обёртке.

    Модель просят вернуть голый JSON, но иногда она оборачивает его в ```json
    или добавляет фразу до объекта. Снимаем обёртку, а если не помогло —
    берём фрагмент от первой { до последней }.
    """
    cleaned = _FENCE_RE.sub("", raw.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start : end + 1])

    raise json.JSONDecodeError("JSON-объект не найден в ответе", raw, 0)


# --- Нормализация цитат ---------------------------------------------------

_QUOTES = "«»\"'“”„‟‘’`"
_DASHES = "—–‑−-"
_TRANS = str.maketrans({c: "" for c in _QUOTES} | {c: " " for c in _DASHES})


def normalize(text: str) -> str:
    """Приводит текст к виду, пригодному для сверки цитаты с ответом.

    Схлопывает пробелы и переносы, приводит к нижнему регистру, убирает
    кавычки и тире. Без этого честные ответы резались бы из-за того, что
    модель поставила другой тип кавычек или заменила дефис на тире.
    """
    return " ".join(text.translate(_TRANS).lower().split())
