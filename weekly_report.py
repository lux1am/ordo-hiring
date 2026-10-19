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
    Recipient,
    missing_secrets,
    setup_logging,
    telegram_recipients,
)
from app.llm import client, parse_json, text_of
from app.retry import call_with_backoff

log = logging.getLogger("weekly_report")

TELEGRAM_API = "https://api.telegram.org"

# Разметка HTML, а не Markdown: таблицы Телеграм не рендерит ни в одной из
# них, и колонки в сводке держатся пробелами внутри <pre>. Всё, что приходит
# извне — названия каналов, текст модели, — экранируется в app/report.py.
PARSE_MODE = "HTML"

MODEL_FAILED_LINE = "Выводы собрать не удалось."

# Слова, по которым отказ узнаётся как «дело в теме, а не в чате». Телеграм
# отвечает то «message thread not found», то «TOPIC_CLOSED» — общего кода у
# этих ответов нет, есть только 400 и слово про тему.
TOPIC_MARKERS = ("thread", "topic")


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


def conclusions(numbers: str) -> tuple[str, str]:
    """Два раздела выводов от модели: «Нике» и «По вакансии».

    Модель отвечает объектом {"nika": "...", "vacancy": "..."} — просить у
    неё разметку незачем, заголовки ставит код. Не разобралось или не
    ответила — оба раздела пустые, и отчёт уходит одними цифрами: цифры
    важнее формулировок.
    """
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY не задан — отчёт уйдёт одними цифрами")
        return "", ""
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
        parsed = parse_json(text_of(response))
        return str(parsed.get("nika") or ""), str(parsed.get("vacancy") or "")
    except Exception:
        # Сбой модели не отменяет отчёт: цифры посчитаны и уйдут без неё.
        log.exception("модель не собрала выводы — отправляю одни цифры")
        return "", ""


def build_message(cur, prev, nika: str = "", vacancy: str = "") -> str:
    """Сообщение целиком. Цифры собирает код, выводы вписываются в остаток.

    Не ответила модель — на её месте одна строка об этом: читатель должен
    знать, что выводов нет, а не думать, что их не было о чём написать.
    """
    if not nika and not vacancy:
        tail = f"\n\n{MODEL_FAILED_LINE}"
        # Место под эту строку вычитается из потолка заранее: прибавленная
        # после сборки, она однажды вынесла бы сообщение за предел.
        return report.message(
            cur, prev, limit=report.MESSAGE_LIMIT - len(tail)
        ) + tail
    return report.message(cur, prev, nika, vacancy)


# ------------------------------- отправка ---------------------------------

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


def payload_for(recipient: Recipient, text: str) -> dict:
    """Тело запроса к sendMessage. Тема группы — отдельным полем."""
    body = {
        "chat_id": recipient.chat_id,
        "text": text,
        "parse_mode": PARSE_MODE,
        "disable_web_page_preview": True,
    }
    if recipient.thread_id is not None:
        body["message_thread_id"] = recipient.thread_id
    return body


def describe(recipient: Recipient) -> str:
    """Получатель для лога: «-1004367079334, тема 12»."""
    if recipient.thread_id is None:
        return recipient.chat_id
    return f"{recipient.chat_id}, тема {recipient.thread_id}"


def attempt(recipient: Recipient, text: str) -> tuple[bool, str, int]:
    """Одна попытка отправки: (удалось, описание ошибки, код ответа).

    Логов здесь нет намеренно: решение, ошибка это или повод повторить,
    принимается уровнем выше — и пишет в лог оно же, по одной строке на
    получателя, а не по одной на попытку.
    """
    try:
        answer = post("sendMessage", payload_for(recipient, text))
    except urllib.error.HTTPError as e:
        # В теле отказа Телеграм объясняет причину — она и нужна.
        return False, e.read().decode("utf-8", "replace")[:300], e.code
    except Exception as err:
        return False, repr(err), 0

    if not answer.get("ok"):
        return (
            False,
            str(answer.get("description") or answer),
            int(answer.get("error_code") or 0),
        )
    return True, "", 200


def is_topic_error(error: str, code: int) -> bool:
    """Отказ из-за темы, а не из-за чата?

    Формулировки у Телеграма разные — «message thread not found»,
    «TOPIC_CLOSED», «TOPIC_DELETED», — общее у них то, что это 400 и в
    описании есть слово про тему. Код 0 — это отказ без кода (оборванная
    связь, странный ответ прокси): если и в нём говорится про тему, повтор
    без неё всё равно уместен.
    """
    if code not in (0, 400):
        return False
    low = error.lower()
    return any(marker in low for marker in TOPIC_MARKERS)


def send_to(recipient: Recipient, text: str) -> bool:
    """Отправка одному получателю. Сообщение целиком, одним запросом:
    потолок в 1500 знаков заведомо меньше предела Телеграма, а резать
    HTML посередине тега нельзя — он вернул бы ошибку разбора.

    Отказ из-за темы не считается концом: тот же чат получит отчёт в общий
    поток. Отчёт не в своей ветке хуже отчёта в своей, но несравнимо лучше
    отчёта, которого нет, — а неверный или закрытый номер темы обнаружится
    ровно в понедельник утром, когда править его некому.
    """
    who = describe(recipient)
    ok, error, code = attempt(recipient, text)

    if not ok and recipient.thread_id is not None and is_topic_error(error, code):
        log.warning(
            "получатель %s: Телеграм отказал из-за темы (%s) — повторяю без неё",
            who, error,
        )
        ok, error, code = attempt(Recipient(recipient.chat_id), text)
        if ok:
            log.warning(
                "получатель %s: отчёт ушёл в общий поток — тема %s не принята, "
                "поправьте номер в TELEGRAM_CHAT_IDS",
                recipient.chat_id, recipient.thread_id,
            )
            return True

    if not ok:
        log.error("получатель %s: отправка не удалась (%s) — %s", who, code, error)
        return False

    log.info("получатель %s: отчёт отправлен", who)
    return True


def broadcast(text: str) -> tuple[int, int]:
    """Рассылка всем. Сбой одного получателя не отменяет остальных."""
    sent = failed = 0
    for recipient in telegram_recipients():
        if send_to(recipient, text):
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

    recipients = telegram_recipients()
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

    nika, vacancy = ("", "") if args.no_model else conclusions(numbers)
    message = build_message(current, previous, nika, vacancy)
    log.info(
        "сообщение собрано: %d знаков из %d", len(message), report.MESSAGE_LIMIT
    )

    if args.dry_run:
        # Печатается и то, что уйдёт в Телеграм, и то, что видела модель:
        # первое — чтобы посмотреть на отчёт, второе — чтобы понять, из
        # каких чисел он собран.
        print("\n" + message + "\n", flush=True)
        print("--- что получила модель ---\n" + numbers + "\n", flush=True)
        log.info(
            "сухой прогон: ничего не отправлено, получателей в настройках %d",
            len(recipients),
        )
        return 0

    sent, failed = broadcast(message)
    log.info("отчёт за %s: отправлен %d, не доставлен %d",
             report.period_title(current.period), sent, failed)
    if sent == 0:
        log.error("отчёт не получил никто")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
