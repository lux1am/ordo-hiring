"""Ограничение частоты отправок с одного адреса.

Используется только формой на FastAPI. В Apps Script аналога нет: адрес
клиента там недоступен — см. apps-script/README.md, «Чего в Apps Script нет».


Счётчик в памяти процесса. На Render Free это один инстанс, и переживать
рестарт счётчику незачем: при перезапуске окно просто начинается заново.
"""
import logging
import threading
import time

from app.config import RATE_LIMIT_PER_HOUR

log = logging.getLogger(__name__)

WINDOW_SEC = 3600
_hits: dict[str, list[float]] = {}
_lock = threading.Lock()


def allow(ip: str) -> bool:
    """True, если с этого адреса ещё можно отправлять."""
    if not ip:
        return True
    now = time.time()
    with _lock:
        fresh = [t for t in _hits.get(ip, []) if now - t < WINDOW_SEC]
        if len(fresh) >= RATE_LIMIT_PER_HOUR:
            _hits[ip] = fresh
            log.warning("превышен лимит отправок с адреса %s: %d за час", ip, len(fresh))
            return False
        fresh.append(now)
        _hits[ip] = fresh

        # Чистим протухшие записи, чтобы словарь не рос бесконечно.
        if len(_hits) > 10000:
            for key in [k for k, v in _hits.items() if not v or now - v[-1] > WINDOW_SEC]:
                _hits.pop(key, None)
    return True


def client_ip(request) -> str:
    """Адрес клиента с учётом прокси Render.

    Render терминирует TLS у себя, поэтому реальный адрес приходит в
    X-Forwarded-For; request.client.host здесь был бы адресом прокси.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
