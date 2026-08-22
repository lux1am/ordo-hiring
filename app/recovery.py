"""Возврат зависших строк в обработку.

Если процесс упал или Render передеплоился посреди обработки, строка
остаётся в processing навсегда и никем не подбирается — кандидат молча
теряется. Здесь реестр захваченных строк, разбор зависших и повторный
запуск обработки.
"""
import logging
import threading
import time

from app import sheets
from app.config import STATUS_NEW, STUCK_SCAN_INTERVAL_SEC, STUCK_TIMEOUT_SEC

log = logging.getLogger(__name__)

# row -> момент захвата этим процессом. Живёт только в памяти: после
# перезапуска реестр пуст, и все processing в таблице по определению
# осиротевшие — их и подбирает recover_on_startup.
_claimed: dict[int, float] = {}
_lock = threading.Lock()


def mark_claimed(row: int) -> None:
    with _lock:
        _claimed[row] = time.time()


def mark_released(row: int) -> None:
    with _lock:
        _claimed.pop(row, None)


def _is_stuck(row: int, now: float) -> bool:
    """Строка зависла, если её захватил мёртвый процесс либо этот, но давно."""
    with _lock:
        claimed_at = _claimed.get(row)
    if claimed_at is None:
        return True                                    # чужая или осиротевшая
    return now - claimed_at > STUCK_TIMEOUT_SEC


def recover(startup: bool = False) -> list[int]:
    """Возвращает зависшие строки в new и заново ставит их в обработку.

    На старте процесса считаем зависшими все processing без исключения:
    новый процесс ничего не обрабатывает, значит эти строки остались от
    прошлой жизни приложения.
    """
    try:
        rows = sheets.rows_in_processing()
    except Exception:
        log.exception("не удалось просканировать лист на зависшие строки")
        return []

    now = time.time()
    stuck = [r for r in rows if startup or _is_stuck(r, now)]
    if not stuck:
        return []

    log.warning(
        "найдено зависших строк: %d (%s) — возвращаю в %s",
        len(stuck), ", ".join(map(str, stuck)), STATUS_NEW,
    )

    revived: list[int] = []
    for row in stuck:
        try:
            sheets.set_status(row, STATUS_NEW)
            mark_released(row)
            revived.append(row)
        except Exception:
            log.exception("строка %d: не удалось вернуть в %s", row, STATUS_NEW)

    # Импорт здесь, а не сверху: pipeline импортирует recovery, и на уровне
    # модуля это дало бы циклический импорт.
    from app.pipeline import process_row

    for row in revived:
        threading.Thread(
            target=process_row, args=(row,), name=f"recover-{row}", daemon=True
        ).start()

    return revived


def _loop(stop: threading.Event) -> None:
    while not stop.wait(STUCK_SCAN_INTERVAL_SEC):
        try:
            recover()
        except Exception:
            log.exception("сканер зависших строк упал, продолжаю по расписанию")


def start_scanner() -> threading.Event:
    """Запускает фоновый скан раз в STUCK_SCAN_INTERVAL_SEC (по умолчанию 5 минут)."""
    stop = threading.Event()
    threading.Thread(target=_loop, args=(stop,), name="stuck-scanner", daemon=True).start()
    log.info("сканер зависших строк запущен, интервал %d сек", STUCK_SCAN_INTERVAL_SEC)
    return stop
