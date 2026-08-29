"""Конвейер обработки анкеты.

Вход всегда — номер строки, а не объект в памяти: тогда обычная обработка
свежей анкеты и восстановление зависшей строки идут одним и тем же кодом.
"""
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app import checks, ru, sheets
from app.config import (
    STATUS_DONE,
    STATUS_MANUAL,
    STATUS_NEW,
    STATUS_PROCESSING,
    TZ_OFFSET_HOURS,
)
from app.models import OPEN_FIELDS, QKEYS, STOP_FIELDS, Submission
from app.profile import pack_markers
from app.recovery import mark_claimed, mark_released
from app.scorer import ScoringFailed, score
from app.verdict import decide, screening_additions

log = logging.getLogger(__name__)

NOT_IMPLEMENTED_NOTIFIED = ""   # TODO: уведомления добавляются отдельным шагом
SCORER_FAILED_RU = "оценка не выполнена, проверить вручную"

# Замки по номеру строки: два потока не должны захватывать одну строку.
_row_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _row_lock(row: int) -> threading.Lock:
    with _locks_guard:
        return _row_locks.setdefault(row, threading.Lock())


def now_local() -> datetime:
    return datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ------------------------- сборка строки анкеты ---------------------------

def build_form_row(sub: Submission, submission_id: str) -> list:
    """34 значения для зоны A..AH, строго в порядке HEADERS."""
    t = sub.telemetry
    row: list = [
        submission_id,
        now_local().strftime("%Y-%m-%d %H:%M:%S"),
        sub.fio,
        sub.telegram,
        sub.resume_url,
        "Да" if sub.pdn_consent else "Нет",
    ]
    row += [sub.stops.get(f, "") for f in STOP_FIELDS]
    row += [sub.answers.get(f, "") for f in OPEN_FIELDS]
    row += [t.field(f).paste_pct for f in OPEN_FIELDS]
    row += [round(t.field(f).speed_cpm, 1) for f in OPEN_FIELDS]
    row += [t.field(f).backspaces for f in OPEN_FIELDS]
    row += [t.tab_switches, round(t.total_time_sec)]
    return row


def build_result_row(
    *,
    ai_style=None,
    ai_markers=None,
    profile=None,
    evidence=None,
    screening=None,
    flags=None,
    verdict="",
    status="",
    conditions="",
    decision="",
    ai_text="",
    flags_text="",
) -> list:
    """12 значений для зоны AO..AZ, строго в порядке HEADERS.

    В ai_markers (AP) едут две вещи сразу: признаки машинного текста от
    модели и ответы кандидата на вопросы о мотивации. Отдельной колонки под
    ответы в раскладке нет, а терять их нельзя — см. profile.pack_markers.
    """
    return [
        "" if ai_style is None else ai_style,
        pack_markers(profile, ai_markers),
        json.dumps(evidence, ensure_ascii=False) if evidence else "",
        "\n".join(screening or []),
        ", ".join(flags or []),
        verdict,
        status,
        NOT_IMPLEMENTED_NOTIFIED,
        conditions,
        decision,
        ai_text,
        flags_text,
    ]


# ---------------------------- обработка -----------------------------------

# Исходы обработки — из них складывается сводка прогона.
OUTCOME_BUSY = "busy"        # строку уже обрабатывает другой поток
OUTCOME_SKIPPED = "skipped"  # статус не new, обрабатывать нечего
OUTCOME_FAILED = "failed"    # упало исключение, строка осталась в processing


def process_row(row: int) -> str:
    """Обрабатывает одну строку и возвращает исход.

    Возврат нужен для сводки прогона: в варианте с Apps Script логи
    GitHub Actions — единственное окно наблюдения за конвейером.
    """
    lock = _row_lock(row)
    if not lock.acquire(blocking=False):
        log.info("строка %d уже обрабатывается в этом процессе, пропускаю", row)
        return OUTCOME_BUSY
    try:
        if not _claim(row):
            return OUTCOME_SKIPPED
        return _process(row)
    except Exception:
        # Строка остаётся в processing и будет подобрана сканером зависших.
        log.exception("строка %d: обработка упала", row)
        return OUTCOME_FAILED
    finally:
        mark_released(row)
        lock.release()


def _claim(row: int) -> bool:
    """Переводит строку из new в processing. False — обрабатывать не нужно."""
    try:
        status = sheets.read_status(row)
    except Exception:
        log.exception("строка %d: не удалось прочитать статус", row)
        return False

    if status != STATUS_NEW:
        log.info("строка %d: статус «%s», повторная обработка запрещена", row, status)
        return False

    sheets.set_status(row, STATUS_PROCESSING)
    mark_claimed(row)
    log.info("строка %d: взята в обработку", row)
    return True


def _process(row: int) -> str:
    form = sheets.read_form(row)
    answers = {f: str(form.get(f, "")).strip() for f in OPEN_FIELDS}
    stops = {f: str(form.get(f, "")).strip() for f in STOP_FIELDS}
    # Флаги телеметрии и вопросов-флагов записаны в AS ещё при сабмите —
    # вычитываем и сливаем со своими. Без этого запись зоны AO..AZ затёрла
    # бы MOBILE, PASTED и всё, что дали вопросы о мотивации.
    flags = sheets.read_flags(row)
    profile = sheets.read_profile(row)
    rejected = checks.stop_failures(stops)

    # 1. Стоп-вопросы. Любой непроходной вариант — отказ без единого
    # обращения к модели.
    if rejected:
        conditions = ru.conditions_ru(rejected)
        log.info("строка %d: отказ по стоп-вопросам — %s", row, conditions)
        sheets.write_result(
            row,
            scores=None,
            result=build_result_row(
                flags=flags,
                profile=profile,
                verdict="C",
                status=STATUS_DONE,
                conditions=conditions,
                decision=ru.decision_ru("C"),
                ai_text="Стиль ИИ не оценивался",
                flags_text=ru.flags_ru(flags, ["отказ по условиям работы"], profile),
            ),
        )
        return STATUS_DONE

    conditions = ru.conditions_ru([])

    # 2. Скоринг. Все пять баллов ставит модель: числовых полей, которые
    # можно было бы посчитать кодом, в анкете больше нет.
    try:
        result, zeroed = score(answers)
    except ScoringFailed as e:
        log.error("строка %d: скоринг не удался — %s", row, e)
        _write_manual(
            row,
            flags=flags,
            profile=profile,
            conditions=conditions,
            reasons=[SCORER_FAILED_RU, f"техническая причина: {e}"],
        )
        return STATUS_MANUAL

    # 3. Вердикт. Понижают A только флаги мотивации и высокий стиль ИИ.
    v = decide(scores=result.scores, flags=flags, ai_style=result.ai_style)

    scores = [result.scores[k] for k in QKEYS]
    reasons = v.reasons + zeroed
    # Телеметрия не понижает вердикт, а даёт четвёртый вопрос на интервью.
    screening = result.screening_questions + screening_additions(flags)

    # Отсюда анкета всегда выходит с готовым вердиктом. Единственный путь в
    # ручную проверку — сбой скорера, он обрабатывается выше и до этого
    # места не доходит.
    sheets.write_result(
        row,
        scores=scores,
        result=build_result_row(
            ai_style=result.ai_style,
            ai_markers=result.ai_markers,
            profile=profile,
            evidence=result.evidence,
            screening=screening,
            flags=flags,
            verdict=v.verdict,
            status=STATUS_DONE,
            conditions=conditions,
            decision=ru.decision_ru(v.verdict),
            ai_text=ru.ai_ru(result.ai_style, result.ai_markers),
            flags_text=ru.flags_ru(flags, reasons, profile),
        ),
    )
    log.info(
        "строка %d: готово — вердикт %s, сумма %d", row, v.verdict, v.total
    )
    return STATUS_DONE


def _write_manual(
    row: int, *, flags: list[str], profile: dict, conditions: str, reasons: list[str]
) -> None:
    """Ручная проверка без баллов: модель не отработала."""
    sheets.write_result(
        row,
        scores=None,
        result=build_result_row(
            flags=flags,
            profile=profile,
            verdict="B",
            status=STATUS_MANUAL,
            conditions=conditions,
            decision=ru.decision_ru("B", is_manual=True),
            ai_text="Стиль ИИ не оценивался",
            flags_text=ru.flags_ru(flags, reasons, profile),
        ),
    )
