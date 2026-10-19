#!/usr/bin/env python3
"""Лист «Сводка для Поли»: та же сводка, что «Эффективность», но числами.

Эту выгрузку читает внешний бот — не человек. Он берёт блок, берёт колонку,
берёт число, поэтому здесь нет ни одной формулы: всё посчитано кодом и
записано значениями. Формулу бот прочитать не может — через API ячейка с
формулой приходит либо её текстом, либо последним пересчитанным значением,
а когда таблица пересчитается в следующий раз, не знает никто.

Три блока сверху вниз, у каждого подпись и шапка:

    КАНАЛЫ   строка на канал за всё время: размещений, откликов, A/B/C,
             средний балл, отдача первого и последнего размещения и дата
             последнего
    ДНИ      строка на день за последние 14 календарных дней, свежий сверху
    ИТОГО    «показатель — значение»: сколько кандидатов в базе, сколько за
             неделю, сколько сильных ещё не разобрано и когда сводка собрана

Персональных данных в листе нет и быть не должно: ни ФИО, ни телеграмов, ни
ссылок на резюме, ни текстов ответов, ни заметок. Только числа, названия
каналов и даты. Это единственный лист книги, про который известно заранее,
что его прочитает кто-то кроме Ники, и отдавать наружу анкеты целиком
никто не договаривался. Колонка «Что было дальше» читается ровно на
«заполнена или нет» — её текст в сводку не попадает. Закреплено тестом
tests/test_digest.py.

Числа считаются по тем же правилам, что формулы «Эффективности»
(placements.py), с одной намеренной разницей: средний балл берётся по
оценённым анкетам, а не по всем дошедшим до баллов. Формула в таблице так
не умеет, и у неё анкета, ещё не прошедшая скоринг, попадает в знаменатель
нулём — средний балл канала падает на время обработки. Здесь же он
считается ровно по той базе, что в еженедельном отчёте.

Лист скрытый и целиком принадлежит коду: заполнять в нём руками нечего, и
пересборка переписывает его сверху донизу, ужимая до нужного размера.
Остаться лишней строке нельзя — бот прочитал бы вчерашние числа как
сегодняшние.

Собирается из sync_questions.py вместе с листами учёта и обновляется в
конце каждого прогона process_new.py.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import gspread

import placements
from HEADERS import col_letter
from app import report, sheets
from app.config import (
    DIGEST_SHEET,
    OUTREACH_SHEET,
    PLACEMENTS_SHEET,
    TZ_OFFSET_HOURS,
    WORKSHEET_NAME,
)

log = logging.getLogger("digest")

# --------------------------- раскладка листа ------------------------------
#
# Подписи блоков — они же якоря: по ним блок находит и бот, и тест.

CHANNELS_TITLE = "КАНАЛЫ"
CHANNELS_HEADER = [
    "Канал",
    "Размещений всего",
    "Откликов всего",
    "A",
    "B",
    "C",
    "Средний балл",
    "Откликов на первом размещении",
    "Откликов на последнем размещении",
    "Дата последнего размещения",
]

DAYS_TITLE = "ДНИ"
DAYS_HEADER = ["Дата", "Размещений", "Личных касаний", "Откликов", "Из них A"]
# Календарных дней, а не рабочих: пропущенный день — тоже сведения, и
# пропасть из таблицы он не должен.
DAYS_WINDOW = 14

TOTALS_TITLE = "ИТОГО"
TOTALS_HEADER = ["Показатель", "Значение"]
TOTALS_WINDOW = 7

# Ширина листа — по самому широкому блоку. Короткие строки добиваются
# пустотой, см. padded: иначе на месте их хвоста остались бы числа прошлой
# сборки, когда блок был длиннее.
DIGEST_COLS = len(CHANNELS_HEADER)

# Название колонки outcome (BB) так, как она называется в книге.
OUTCOME_COLUMN = "Что было дальше"

TOTAL_CANDIDATES = "Всего кандидатов в базе"
RESPONSES_RECENT = f"Откликов за {TOTALS_WINDOW} дней"
STRONG_UNTOUCHED = f"Уровня A без «{OUTCOME_COLUMN}»"
UNLABELLED_RECENT = f"Откликов без метки за {TOTALS_WINDOW} дней"
UPDATED_AT = "Сводка обновлена"

# Дата — текстом ISO, а не серийным числом Sheets: бот читает значение как
# есть, и «2026-10-19» не зависит ни от локали книги, ни от формата ячейки.
DATE_FORMAT = "%Y-%m-%d"
UPDATED_FORMAT = "%Y-%m-%d %H:%M"

STRONG = "A"


def now_local() -> datetime:
    """Местное время рекрутёра. По нему же считаются последние 7 и 14 дней:
    прогон в 00:30 МСК — это 21:30 UTC предыдущего дня, и окно, посчитанное
    от UTC, разошлось бы с датами в колонке created_at."""
    return datetime.now(timezone(timedelta(hours=TZ_OFFSET_HOURS)))


# ------------------------------- ячейки -----------------------------------

def cell(value: int | float | None) -> int | float | str:
    """Число или пустая ячейка. Ноля там, где числа нет, быть не должно:
    «средний балл 0» и «балл не считался» — разные сведения."""
    return "" if value is None else value


def iso(day: date | None) -> str:
    return day.strftime(DATE_FORMAT) if day else ""


# ------------------------------- КАНАЛЫ -----------------------------------

@dataclass
class Channel:
    """Канал за всё время: сколько в него вложено и что он принёс."""

    channel: str
    placements: int             # размещений всего, с датой и без
    responses: int              # откликов по всем его меткам
    strong: int = 0             # из них A
    arguable: int = 0           # B
    rejected: int = 0           # C
    average: float | None = None
    first_yield: int | None = None   # откликов у самого первого размещения
    last_yield: int | None = None    # и у самого последнего
    last_day: date | None = None     # дата последнего размещения

    def values(self) -> list:
        return [
            self.channel,
            self.placements,
            self.responses,
            self.strong,
            self.arguable,
            self.rejected,
            cell(self.average),
            cell(self.first_yield),
            cell(self.last_yield),
            iso(self.last_day),
        ]


def average(responses: list[report.Response]) -> float | None:
    """Средний балл по оценённым анкетам. Некого оценивать — числа нет.

    Отсеявшиеся на стоп-вопросах до баллов не доходят вовсе, и держать их в
    знаменателе значит занижать отдачу канала тем сильнее, чем честнее он
    отобрал людей. Анкета, ещё не прошедшая скоринг, тоже не в счёт: её
    баллы появятся через минуту, а до тех пор занижали бы средний.
    """
    scores = [r.total for r in responses if r.scored and r.total is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


def by_label(responses: list[report.Response]) -> dict[str, list[report.Response]]:
    """Отклики по метке. Без метки — ни к одному каналу не относятся."""
    out: dict[str, list[report.Response]] = {}
    for r in responses:
        if r.label:
            out.setdefault(r.label, []).append(r)
    return out


def channel_rows(
    responses: list[report.Response], posts: list[report.Placement]
) -> list[Channel]:
    """Строка на канал, отсортированные по отдаче.

    Размещение без выбранного канала считается тоже — работа была сделана, —
    и собирается в строку «— без канала»: пропав из сводки, его отклики не
    сошлись бы с блоком ИТОГО.
    """
    answers = by_label(responses)

    groups: dict[str, list[report.Placement]] = {}
    for post in posts:
        groups.setdefault(post.channel or report.NO_CHANNEL, []).append(post)

    rows = []
    for name, group in groups.items():
        answered = [r for post in group for r in answers.get(post.label, [])]
        # Отдача первого и последнего размещения считается только по тем, у
        # которых есть дата: без неё порядок размещений неизвестен.
        dated = sorted((p for p in group if p.day), key=lambda p: p.day)
        verdicts = [r.verdict for r in answered]
        rows.append(
            Channel(
                channel=name,
                placements=len(group),
                responses=len(answered),
                strong=verdicts.count("A"),
                arguable=verdicts.count("B"),
                rejected=verdicts.count("C"),
                average=average(answered),
                first_yield=len(answers.get(dated[0].label, [])) if dated else None,
                last_yield=len(answers.get(dated[-1].label, [])) if dated else None,
                last_day=dated[-1].day if dated else None,
            )
        )
    return sorted(rows, key=lambda c: (-c.responses, -c.placements, c.channel))


# -------------------------------- ДНИ -------------------------------------

@dataclass
class Day:
    """День: сколько работы сделано и что она принесла."""

    day: date
    placements: int = 0
    outreach_sent: int = 0
    responses: int = 0
    strong: int = 0

    def values(self) -> list:
        return [
            iso(self.day),
            self.placements,
            self.outreach_sent,
            self.responses,
            self.strong,
        ]


def window(today: date, days: int) -> list[date]:
    """Последние `days` календарных дней, сегодняшний первым."""
    return [today - timedelta(days=i) for i in range(days)]


def day_rows(
    today: date,
    responses: list[report.Response],
    posts: list[report.Placement],
    touches: list[report.Outreach],
) -> list[Day]:
    """Четырнадцать строк, свежая сверху.

    Свежая сверху намеренно: бот, которому нужен вчерашний день, читает
    вторую строку блока, а не ищет конец таблицы. День без работы и без
    откликов остаётся строкой нулей — его отсутствие читалось бы как
    отсутствие дня.
    """
    days = window(today, DAYS_WINDOW)
    rows = {day: Day(day=day) for day in days}

    for post in posts:
        if post.day in rows:
            rows[post.day].placements += 1
    for touch in touches:
        if touch.day in rows:
            rows[touch.day].outreach_sent += touch.sent
    for r in responses:
        if r.day in rows:
            rows[r.day].responses += 1
            if r.verdict == STRONG:
                rows[r.day].strong += 1

    return [rows[day] for day in days]


# -------------------------------- ИТОГО -----------------------------------

def totals(now: datetime, responses: list[report.Response]) -> list[list]:
    """Пять строк «показатель — значение».

    «Уровня A без «Что было дальше»» — счёт за всё время, а не за неделю:
    это список дел. Сильный кандидат, которого никто не отработал месяц
    назад, из него выпадать не должен.
    """
    recent = set(window(now.date(), TOTALS_WINDOW))
    fresh = [r for r in responses if r.day in recent]
    return [
        [TOTAL_CANDIDATES, len(responses)],
        [RESPONSES_RECENT, len(fresh)],
        [
            STRONG_UNTOUCHED,
            sum(1 for r in responses if r.verdict == STRONG and not r.outcome),
        ],
        [UNLABELLED_RECENT, sum(1 for r in fresh if not r.label)],
        [UPDATED_AT, now.strftime(UPDATED_FORMAT)],
    ]


# ---------------------------- содержимое листа ----------------------------

def values(
    now: datetime,
    responses: list[report.Response],
    posts: list[report.Placement],
    touches: list[report.Outreach],
) -> list[list]:
    """Лист целиком, строка за строкой.

    Пустой блок остаётся одной шапкой без строк: подпись «размещений пока
    нет» бот прочитал бы как название канала.
    """
    rows: list[list] = []

    def block(title: str, header: list[str], body: list[list]) -> None:
        if rows:
            rows.append([])        # пустая строка между блоками
        rows.append([title])
        rows.append(list(header))
        rows.extend(body)

    block(
        CHANNELS_TITLE,
        CHANNELS_HEADER,
        [c.values() for c in channel_rows(responses, posts)],
    )
    block(
        DAYS_TITLE,
        DAYS_HEADER,
        [d.values() for d in day_rows(now.date(), responses, posts, touches)],
    )
    block(TOTALS_TITLE, TOTALS_HEADER, totals(now, responses))

    return rows


def padded(rows: list[list]) -> list[list]:
    """Все строки одной ширины.

    Короткая строка оставила бы на месте своего хвоста числа прошлой
    сборки: запись задаёт только те ячейки, которые в неё переданы.
    """
    return [row + [""] * (DIGEST_COLS - len(row)) for row in rows]


# ------------------------------ работа с книгой ---------------------------

def read(book: gspread.Spreadsheet) -> tuple[list, list, list]:
    """Отклики, размещения и касания — теми же читалками, что у отчёта."""
    data = sheets.values_of(book.worksheet(WORKSHEET_NAME))
    posts = sheets.other_sheet(book, PLACEMENTS_SHEET)
    touches = sheets.other_sheet(book, OUTREACH_SHEET)
    return (
        report.read_responses(data),
        report.read_placements(
            posts,
            date_col=placements.PLACEMENT_DATE_COL,
            channel_col=placements.PLACEMENT_CHANNEL_COL,
        ),
        report.read_outreach(
            touches,
            date_col=placements.OUTREACH_DATE_COL,
            sent_col=placements.OUTREACH_SENT_COL,
        ),
    )


def open_digest(book: gspread.Spreadsheet, rows: int) -> gspread.Worksheet:
    """Лист ровно под сводку. В отличие от листов учёта — ужимается.

    Ужать здесь не только можно, но и нужно: руками в этот лист не пишут, а
    строка, оставшаяся от прошлой сборки ниже новых данных, попала бы боту
    как ещё один канал или ещё один день.
    """
    try:
        ws = book.worksheet(DIGEST_SHEET)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=DIGEST_SHEET, rows=rows, cols=DIGEST_COLS)
        log.info("создан лист «%s»", DIGEST_SHEET)
        return ws

    if (ws.row_count, ws.col_count) != (rows, DIGEST_COLS):
        ws.resize(rows=rows, cols=DIGEST_COLS)
    return ws


def rebuild(book: gspread.Spreadsheet, now: datetime | None = None) -> None:
    """Пересобирает сводку целиком — одной записью. Ручного в листе нет.

    Оформления лист не получает вовсе: ни жирной шапки, ни ширины колонок.
    Смотреть на него некому — читает его бот, — а каждое оформление это ещё
    одна запись в минутный лимит Sheets, и уходила бы она каждые десять
    минут, с каждым прогоном process_new.
    """
    rows = padded(values(now or now_local(), *read(book)))
    ws = open_digest(book, len(rows))
    placements.write_values(ws, f"A1:{col_letter(DIGEST_COLS)}{len(rows)}", rows)
    log.info("лист «%s»: записано %d строк значениями", DIGEST_SHEET, len(rows))


def sync(book: gspread.Spreadsheet, now: datetime | None = None) -> None:
    """Заводит лист и сразу его заполняет. Зовётся из sync_questions.py.

    Готовый лист повторная сборка книги не трогает: его обновляет каждый
    прогон process_new, то есть не позже чем через десять минут. Писать то
    же самое ещё раз незачем — записи Sheets идут в один лимит на минуту с
    тремя десятками запросов, которыми собираются остальные листы, и
    повторный запуск скрипта сразу за первым должен в него влезать.
    """
    try:
        book.worksheet(DIGEST_SHEET)
    except gspread.WorksheetNotFound:
        rebuild(book, now)
        return
    log.info(
        "лист «%s» уже есть — обновит его прогон process_new", DIGEST_SHEET
    )


def refresh() -> None:
    """Обновить сводку в боевой книге. Зовётся в конце прогона process_new."""
    rebuild(sheets.worksheet().spreadsheet)


def dry_run() -> None:
    """Печатает раскладку листа в терминал. В книгу не ходит."""
    print(f"\n=== лист «{DIGEST_SHEET}» (скрытый, значениями) ===", flush=True)
    for i, row in enumerate(padded(values(now_local(), [], [], [])), start=1):
        print(f"{i:>3}  " + " | ".join(str(v) for v in row), flush=True)
