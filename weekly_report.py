#!/usr/bin/env python3
"""Еженедельный отчёт по подбору в Телеграм.

Запускается в понедельник в 09:00 МСК из GitHub Actions
(.github/workflows/report.yml) и отчитывается за прошедшую полную неделю:
размещения в каналах, личные касания, отклики и что из них выросло.

Разделение работы жёсткое: цифры считает код (app/report.py), модель
получает готовый блок чисел и формулирует по нему выводы и вопросы
(prompts/report.md). Поэтому ошибиться в числе модель не может — числа в
отчёт попадают из кода, а не из её ответа, и стоят в сообщении выше
её текста.

Отчёт уходит всегда. Нет откликов — уходит с явной строкой «Откликов нет».
Не ответила модель — уходит один блок цифр: цифры важнее формулировок.
Не принял один получатель — остальные всё равно получают, и код возврата
это отражает.

Запуск:

    python3 weekly_report.py             # посчитать, собрать, отправить
    python3 weekly_report.py --dry-run   # напечатать в терминал, не отправлять
    python3 weekly_report.py --no-model  # только цифры, без обращения к модели

Код возврата: 0 — отчёт отправлен (или напечатан при --dry-run), 1 — не
удалось прочитать книгу или отправить ни одному получателю, 2 — не
настроено окружение.
"""
import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import gspread
from gspread.utils import ValueRenderOption

import placements
from app import report, sheets
from app.config import (
    ANTHROPIC_API_KEY,
    BASE_DIR,
    OUTREACH_SHEET,
    PLACEMENTS_SHEET,
    REPORT_MAX_TOKENS,
    REPORT_MODEL,
    TELEGRAM_BOT_TOKEN,
    TZ_OFFSET_HOURS,
    missing_secrets,
    setup_logging,
    telegram_chat_ids,
)
from app.llm import client, text_of
from app.retry import call_with_backoff

log = logging.getLogger("weekly_report")

TELEGRAM_API = "https://api.telegram.org"

# Предел сообщения в Телеграме — 4096 знаков. Режем с запасом и по абзацам:
# отчёт, разорванный посередине строки с числом, читать невозможно.
MESSAGE_LIMIT = 3800

MODEL_FAILED_LINE = (
    "Выводы собрать не удалось: модель не ответила. Цифры выше посчитаны "
    "кодом и верны."
)
SEPARATOR = "—" * 20


def now_local() -> datetime:
    """Местное время рекрутёра. Неделя отчёта считается от него, а не от UTC:
    прогон в 09:00 МСК — это 06:00 UTC, и в воскресенье ночью разница в три
    часа означала бы другую неделю."""
    return datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))


# ------------------------------- чтение книги -----------------------------

def values_of(ws: gspread.Worksheet) -> list[list]:
    """Все значения листа без форматирования.

    UNFORMATTED_VALUE важен для даты: отформатированная приходит строкой в
    том виде, в каком её показывают человеку, и зависит от локали книги.
    Неотформатированная — числом от эпохи Sheets, одинаково всегда.
    """
    return ws.get_values(value_render_option=ValueRenderOption.unformatted)


def other_sheet(book: gspread.Spreadsheet, title: str) -> list[list]:
    """Лист учёта. Нет его — отчёт всё равно уходит, с нулями и жалобой в лог."""
    try:
        return values_of(book.worksheet(title))
    except gspread.WorksheetNotFound:
        log.warning(
            "листа «%s» в книге нет — соберите его: python3 sync_questions.py", title
        )
        return []
    except Exception:
        log.exception("лист «%s» не прочитался — считаю без него", title)
        return []


def read_all() -> tuple[list[list], list[list], list[list]]:
    data = sheets.worksheet()
    book = data.spreadsheet
    return (
        values_of(data),
        other_sheet(book, PLACEMENTS_SHEET),
        other_sheet(book, OUTREACH_SHEET),
    )


def collect(today: date) -> tuple[report.WeekStats, report.WeekStats, str]:
    """Цифры прошедшей недели и предыдущей. Третьим — готовый блок текста."""
    data, placement_rows, outreach_rows = read_all()

    responses = report.read_responses(data)
    posts = report.read_placements(
        placement_rows,
        date_col=placements.PLACEMENT_DATE_COL,
        channel_col=placements.PLACEMENT_CHANNEL_COL,
    )
    touches = report.read_outreach(
        outreach_rows,
        date_col=placements.OUTREACH_DATE_COL,
        sent_col=placements.OUTREACH_SENT_COL,
    )
    log.info(
        "прочитано: откликов %d, размещений %d, дней с касаниями %d",
        len(responses), len(posts), len(touches),
    )

    period = report.last_week(today)
    current = report.stats(period, responses, posts, touches)
    previous = report.stats(period.shifted(-1), responses, posts, touches)
    return current, previous, report.numbers_text(current, previous)


# -------------------------------- модель ----------------------------------

def load_prompt() -> str:
    return (BASE_DIR / "prompts" / "report.md").read_text(encoding="utf-8")


def conclusions(numbers: str) -> str:
    """Выводы и вопросы от модели. Пустая строка — значит не получилось."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY не задан — отчёт уйдёт одними цифрами")
        return ""
    try:
        response = call_with_backoff(
            lambda: client().messages.create(
                model=REPORT_MODEL,
                max_tokens=REPORT_MAX_TOKENS,
                system=load_prompt(),
                messages=[{"role": "user", "content": numbers}],
            ),
            label="отчёт",
        )
        return text_of(response).strip()
    except Exception:
        # Сбой модели не отменяет отчёт: цифры посчитаны и уйдут без неё.
        log.exception("модель не собрала выводы — отправляю одни цифры")
        return ""


def build_message(numbers: str, text: str, period_title: str) -> str:
    """Сообщение целиком: заголовок, цифры от кода, выводы от модели."""
    tail = text or MODEL_FAILED_LINE
    return f"Отчёт по подбору за неделю {period_title}\n\n{numbers}\n\n{SEPARATOR}\n\n{tail}"


# ------------------------------- отправка ---------------------------------

def chunks(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Разбивка по абзацам: строка с числом не должна рваться пополам."""
    out: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            out.append(current)
        # Абзац, который и сам не влезает, режем по строкам.
        while len(paragraph) > limit:
            cut = paragraph.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(paragraph[:cut])
            paragraph = paragraph[cut:].lstrip("\n")
        current = paragraph
    if current:
        out.append(current)
    return out or [""]


def post(method: str, payload: dict) -> dict:
    """Запрос к Bot API. Вынесен отдельно — его подменяют тесты."""
    request = urllib.request.Request(
        f"{TELEGRAM_API}/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def send_to(chat_id: str, text: str) -> bool:
    """Отправка одному получателю. Форматирования нет намеренно: в отчёте
    есть и подчёркивания, и звёздочки, и разметка Телеграма споткнулась бы
    на них, вернув ошибку вместо сообщения."""
    for part in chunks(text):
        try:
            answer = post(
                "sendMessage",
                {"chat_id": chat_id, "text": part, "disable_web_page_preview": True},
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            log.error("получатель %s: Телеграм ответил %s — %s", chat_id, e.code, body)
            return False
        except Exception:
            log.exception("получатель %s: отправка не удалась", chat_id)
            return False

        if not answer.get("ok"):
            log.error("получатель %s: Телеграм отказал — %s", chat_id, answer)
            return False
    log.info("получатель %s: отчёт отправлен", chat_id)
    return True


def broadcast(text: str) -> tuple[int, int]:
    """Рассылка всем. Сбой одного получателя не отменяет остальных."""
    sent = failed = 0
    for chat_id in telegram_chat_ids():
        if send_to(chat_id, text):
            sent += 1
        else:
            failed += 1
    return sent, failed


# ------------------------------- точка входа ------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Еженедельный отчёт по подбору")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="напечатать отчёт в терминал и ничего не отправлять",
    )
    parser.add_argument(
        "--no-model", action="store_true",
        help="не обращаться к модели: только цифры",
    )
    args = parser.parse_args(argv)

    setup_logging()

    # Модель и получатели нужны не всегда: сухой прогон обходится таблицей.
    required = ["GOOGLE_CREDENTIALS_JSON", "SHEET_ID"]
    missing = [s for s in missing_secrets() if s in required]
    if missing:
        log.error("не заданы переменные окружения: %s", ", ".join(missing))
        return 2

    recipients = telegram_chat_ids()
    if not args.dry_run:
        if not TELEGRAM_BOT_TOKEN:
            log.error("не задан TELEGRAM_BOT_TOKEN — отправлять нечем")
            return 2
        if not recipients:
            log.error("не задан TELEGRAM_CHAT_IDS — отправлять некому")
            return 2

    try:
        current, previous, numbers = collect(now_local().date())
    except Exception:
        log.exception("не удалось собрать цифры отчёта")
        return 1

    text = "" if args.no_model else conclusions(numbers)
    message = build_message(numbers, text, current.period.title())

    if args.dry_run:
        print("\n" + message + "\n", flush=True)
        log.info(
            "сухой прогон: ничего не отправлено, получателей в настройках %d",
            len(recipients),
        )
        return 0

    sent, failed = broadcast(message)
    log.info("отчёт за %s: отправлен %d, не доставлен %d",
             current.period.title(), sent, failed)
    if sent == 0:
        log.error("отчёт не получил никто")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
