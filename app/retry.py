"""Экспоненциальный бэкофф поверх Anthropic API.

Собственные ретраи ставятся на клиент с max_retries=0 — иначе задержки
SDK перемножатся с этими и волна анкет будет разгребаться часами.
"""
import logging
import time
from typing import Callable, TypeVar

import anthropic

from app.config import API_RETRY_DELAYS

log = logging.getLogger(__name__)
T = TypeVar("T")

# Ошибки, которые имеет смысл повторять: перегрузка, лимит, сеть, таймаут.
RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)


class RetriesExhausted(Exception):
    """Все попытки исчерпаны. Вызывающий переводит анкету в manual."""

    def __init__(self, label: str, attempts: int, last: Exception):
        super().__init__(f"{label}: {attempts} попыток исчерпано, последняя ошибка: {last!r}")
        self.label = label
        self.attempts = attempts
        self.last = last


def call_with_backoff(fn: Callable[[], T], *, label: str, sleep=time.sleep) -> T:
    """Зовёт fn с задержками из API_RETRY_DELAYS (по умолчанию 2/8/30 сек).

    Неретраибельные ошибки (400, 401, 404) пробрасываются сразу — ждать
    их бессмысленно, это ошибка конфигурации, а не нагрузки.
    """
    delays = API_RETRY_DELAYS
    attempts = len(delays)
    last: Exception | None = None

    for i in range(attempts):
        try:
            return fn()
        except RETRYABLE as e:
            last = e
            if i == attempts - 1:
                break
            wait = delays[i]
            log.warning(
                "%s: попытка %d/%d не удалась (%s), повтор через %.0f сек",
                label, i + 1, attempts, type(e).__name__, wait,
            )
            sleep(wait)
        except anthropic.APIStatusError as e:
            log.error("%s: неретраибельная ошибка API %s — %s", label, e.status_code, e)
            raise

    raise RetriesExhausted(label, attempts, last)  # type: ignore[arg-type]
