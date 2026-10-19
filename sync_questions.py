#!/usr/bin/env python3
"""Лист «Карточка»: одна анкета целиком, для скрининга по телефону.

Лист «Данные» — это склад: 54 колонки кодами, JSON в ячейках, ответ и балл
за один вопрос в разных концах строки. Разговаривать с кандидатом, водя
глазами по горизонтали, невозможно. «Карточка» разворачивает одну анкету
вертикально: сверху фамилия с выпадающим списком, ниже четыре блока —
условия, мотивация, ответы, вопросы к скринингу.

Всё, кроме формулировок вопросов, подтягивается формулами по фамилии из
A1. Смена фамилии в списке мгновенно перестраивает карточку — скрипт для
этого запускать не нужно. Формулировки берутся из questions.json при сборке
и ложатся текстом: они меняются вместе со схемой анкеты, а не с каждым
кандидатом.

Скрипт заодно убирает прежнюю затею — лист «Вопросы» и примечания к шапке
«Данных» — и приводит в порядок видимость листов: склад прячет, рабочие
листы показывает.

Скрипт идемпотентный: лист собирается заново поверх прежнего, а не
дополняется. Выбранная в A1 фамилия при пересборке сохраняется. Запускать
можно сколько угодно раз.

Вызывается сам в конце apply_headers.py — чтобы при смене схемы анкеты
карточка обновлялась вместе с шапкой и не отставала от формулировок.

О печати. Sheets API не умеет задавать ориентацию, область печати и масштаб:
среди его запросов нет ни одного про печать. Поэтому скрипт делает то, что в
его власти, — ужимает лист ровно до карточки, и тогда «Текущий лист» в
диалоге печати и есть область печати, — а в конце печатает ссылку на выгрузку
в PDF, где книжная ориентация и вписывание в страницу заданы параметрами.

Запуск:

    python3 sync_questions.py            # пересобрать карточку
    python3 sync_questions.py --dry-run  # напечатать в терминал, книгу не трогать

Код возврата: 0 — готово, 1 — сбой при работе с книгой, 2 — не настроено
окружение.
"""
import argparse
import logging
import re
import sys

import gspread
from gspread.utils import ValidationConditionType

from HEADERS import FIRST_DATA_ROW, HEADERS, col_letter, index_of
from app import checks, ru, sheets
from app.config import (
    SHEET_ID,
    WORKSHEET_NAME,
    load_questions,
    missing_secrets,
    setup_logging,
)
from app.models import QKEYS
from app.profile import STOP_SEP
from app.verdict import TELEMETRY_FLAGS, screening_additions
from archive_old import ARCHIVE_TITLE

log = logging.getLogger("sync_questions")

CARD_TITLE = "Карточка"

# Лист прежней затеи — расшифровка анкеты таблицей. Отменён, удаляется.
OLD_SHEET_TITLE = "Вопросы"

# Видимость листов в конце прогона. «Данные» и архив — склад, в них лазают
# по делу; остальные три читает человек.
HIDDEN_SHEETS = (WORKSHEET_NAME, ARCHIVE_TITLE)
VISIBLE_SHEETS = ("Кандидаты", CARD_TITLE, "Как читать")

# Ячейка с фамилией. Все формулы карточки смотрят сюда.
NAME_CELL = "$A$1"
NAME_A1 = "A1"

CARD_COLS = 3

PASS_MARK = "✓"
FAIL_MARK = "✗"

# Максимум по рубрикам: пять открытых вопросов по 3 балла, см. rubric/q*.md.
MAX_PER_QUESTION = 3
MAX_TOTAL = MAX_PER_QUESTION * len(QKEYS)

OPEN_FIELD_RE = re.compile(r"^q(\d+)_text$")

STOPS_TITLE = "УСЛОВИЯ"
FLAGS_TITLE = "МОТИВАЦИЯ И ОЖИДАНИЯ"
ANSWERS_TITLE = "ОТВЕТЫ"
SCREENING_TITLE = "ВОПРОСЫ К СКРИНИНГУ"
CONCERNS_LABEL = "Что смущает"

# Сколько строк отводить под вопросы к скринингу. Модель возвращает ровно
# три — это стережёт валидатор в app/models.py. Ещё одну дописывает
# телеметрия: вставленный текст вердикт не понижает, а превращается в вопрос
# голосом, см. app/verdict.py. Число считается по той же функции, которая
# эти строки и добавляет, — тогда новое правило приедет в карточку само.
MODEL_SCREENING_ROWS = 3
SCREENING_ROWS = MODEL_SCREENING_ROWS + len(
    screening_additions(list(TELEMETRY_FLAGS))
)

# Ширина колонок в знаках: вопрос, ответ, отметка. API принимает пиксели,
# поэтому переводим — Arial 10 в Sheets даёт около семи пикселей на знак.
COLUMN_CHARS = (55, 60, 14)
PIXELS_PER_CHAR = 7


# --------------------------- чистые функции -------------------------------
#
# Формулы собираются здесь целиком и без обращения к книге: тогда карточка
# проверяется тестом, а не разглядыванием ошибок #REF! на боевой таблице.


def stop_questions() -> list[dict]:
    return load_questions()["stop_questions"]


def open_questions() -> list[dict]:
    return load_questions()["open_questions"]


def flag_questions() -> list[dict]:
    return load_questions()["flag_questions"]


def lit(text: str) -> str:
    """Строковый литерал для формулы: кавычки внутри удваиваются."""
    return '"' + str(text).replace('"', '""') + '"'


def json_re(key: str) -> str:
    """Регулярка на строковое значение ключа в JSON-е колонки ai_markers."""
    return r'"%s":\s*"([^"]*)"' % key


def _index(column: str) -> str:
    """INDEX/MATCH по колонке «Данных» для фамилии из A1.

    MATCH берёт первое совпадение: у двух кандидатов с одинаковым ФИО
    карточка покажет того, кто в листе выше. Различать их нечем — телеграм
    и есть первое, что видно в карточке.
    """
    letter = col_letter(index_of(column))
    fio = col_letter(index_of("fio"))
    book = "'%s'!" % WORKSHEET_NAME
    return (
        f"INDEX({book}{letter}:{letter},"
        f"MATCH({NAME_CELL},{book}{fio}:{fio},0))"
    )


def value_of(column: str) -> str:
    """Значение колонки как выражение — без ошибки, когда фамилия не выбрана."""
    return f'IFERROR({_index(column)},"")'


def cell(expression: str) -> str:
    return "=" + expression


def score_column(field: str) -> str:
    """Колонка с баллом за открытый вопрос: q3_text -> score_q3."""
    m = OPEN_FIELD_RE.match(field)
    name = f"score_q{m.group(1)}" if m else ""
    return name if name in HEADERS else ""


# --- Шапка карточки -------------------------------------------------------

def top_rows() -> list[list[str]]:
    """Фамилия, решение, уровень, баллы и телеграм — две строки.

    A1 остаётся пустой: туда пишется выбранная фамилия, и её же перечитывает
    пересборка, чтобы не сбрасывать карточку на чужую.
    """
    verdict = value_of("verdict")
    total = value_of("total_score")
    return [
        [
            "",
            cell(value_of("decision_ru")),
            cell(f'LET(v,{verdict},IF(v="","","Уровень "&v))'),
        ],
        [
            cell(value_of("telegram")),
            "",
            cell(f'LET(v,{total},IF(v="","",v&" из {MAX_TOTAL}"))'),
        ],
    ]


# --- УСЛОВИЯ --------------------------------------------------------------

def stop_mark(row: int, field: str) -> str:
    """Отметка ✓/✗ по проходному набору из checks.STOP_PASS.

    Сравнивается не вся ячейка, а ответ до уточнения: в третьем стоп-вопросе
    к «Да» приписан часовой пояс через тире, и целиком такая ячейка не
    совпала бы ни с одним вариантом.
    """
    allowed = checks.STOP_PASS.get(field, ())
    if not allowed:
        log.warning("у стоп-вопроса %s нет проходного набора в app/checks.py", field)
        return ""
    tests = ",".join(f"EXACT(o,{lit(option)})" for option in allowed)
    return cell(
        f"LET(a,$B{row},"
        f'o,IFERROR(TRIM(INDEX(SPLIT(a,{lit(STOP_SEP.strip())}),1)),""),'
        f'IF(a="","",IF(OR({tests}),{lit(PASS_MARK)},{lit(FAIL_MARK)})))'
    )


# --- МОТИВАЦИЯ И ОЖИДАНИЯ -------------------------------------------------

def flag_answer(question: dict) -> str:
    """Ответ на вопрос-флаг, вынутый из JSON-а колонки ai_markers.

    Отдельных колонок под эти ответы в листе нет — они едут одной ячейкой
    вместе с признаками машинного текста, см. app/profile.py. Формула
    достаёт нужный ключ регуляркой: разбирать JSON в таблице больше нечем.
    """
    field = question["field"]
    markers = value_of("ai_markers")

    if question["type"] == "single":
        return cell(f'IFERROR(REGEXEXTRACT({markers},{lit(json_re(field))}),"")')

    if question["type"] == "multi":
        chosen = r'"%s":\s*\[([^\]]*)\]' % field
        other = json_re(field + "_other")
        return cell(
            f"LET(j,{markers},"
            f'IFERROR(SUBSTITUTE(REGEXEXTRACT(j,{lit(chosen)}),CHAR(34),""),"")'
            # Свой вариант кандидата дописывается к «Другому», иначе в
            # карточке осталось бы слово «Другое» без содержания.
            f'&IFERROR(" — "&REGEXEXTRACT(j,{lit(other)}),""))'
        )

    places = '&", "&'.join(
        f'{lit((row.get("short") or row["label"]) + " ")}'
        f'&IFERROR(REGEXEXTRACT(j,{lit(json_re(row["field"]))}),"—")'
        for row in question["rows"]
    )
    return cell(f'LET(j,{markers},IF(j="","",{places}))')


def flag_rules() -> dict[str, list[tuple[str, str]]]:
    """Правила из app/checks.py: поле вопроса → [(какой ответ, какой флаг)].

    Собрано из тех же констант, по которым флаги и ставятся, — переписать
    вариант в questions.json и забыть про карточку невозможно: несовпадение
    поймает проверка при импорте app/checks.py.
    """
    places = " или ".join(checks.INCOME_LOW_PLACES)
    return {
        "flag_motive": [
            (
                "выбран единственный вариант — "
                + " или ".join(f"«{o}»" for o in checks.MOTIVE_AVOIDANCE),
                checks.FLAG_MOTIVATION_AVOIDANCE,
            )
        ],
        "flag_income": [
            (f"«{checks.INCOME_LOW}»", checks.FLAG_INCOME_LOW),
            (f"«{checks.INCOME_UNREAL}»", checks.FLAG_INCOME_UNREAL),
        ],
        "flag_rank": [
            (
                f"«{rank_label(checks.RANK_STABILITY)}» на 1 месте",
                checks.FLAG_STABILITY_FIRST,
            ),
            (
                f"«{rank_label(checks.RANK_INCOME)}» на {places} месте",
                checks.FLAG_INCOME_NOT_PRIORITY,
            ),
        ],
        "flag_meetings": [
            (f"«{checks.MEETINGS_UNDERESTIMATE}»", checks.FLAG_UNDERESTIMATE),
            (f"«{checks.MEETINGS_PESSIMIST}»", checks.FLAG_PESSIMIST),
        ],
        "flag_goals": [(f"«{checks.GOALS_UNKNOWN}»", checks.FLAG_NO_GOALS)],
    }


def rank_label(field: str) -> str:
    """Подпись строки вопроса-ранжирования по имени её поля."""
    for question in flag_questions():
        for row in question.get("rows", []):
            if row["field"] == field:
                return row["label"]
    return field


def flag_mark(field: str) -> str:
    """Название сработавшего флага по-русски. Не сработал — пусто.

    Смотрит в готовую колонку flags, а не пересчитывает правило заново:
    флаг ставит app/checks.py при обработке, и карточка обязана показывать
    его решение, а не своё мнение о нём.
    """
    rules = flag_rules().get(field)
    if not rules:
        log.warning("у вопроса-флага %s нет правил в app/checks.py", field)
        return ""
    expression = '""'
    for _, flag in reversed(rules):
        name = ru.FLAG_NAMES.get(flag, flag)
        pattern = r"\b%s\b" % flag
        expression = f"IF(REGEXMATCH(f,{lit(pattern)}),{lit(name)},{expression})"
    return cell(f'LET(f,{value_of("flags")},IF(f="","",{expression}))')


# --- ОТВЕТЫ ---------------------------------------------------------------

def open_score(field: str) -> str:
    """Балл за открытый вопрос в виде «2 из 3»."""
    column = score_column(field)
    if not column:
        log.warning("у открытого вопроса %s нет колонки с баллом", field)
        return ""
    return cell(f'LET(v,{value_of(column)},IF(v="","",v&" из {MAX_PER_QUESTION}"))')


# --- ВОПРОСЫ К СКРИНИНГУ --------------------------------------------------

def screening_answer(number: int) -> str:
    """Строка из колонки AR: вопросы лежат там через перевод строки."""
    return cell(
        f'IFERROR(INDEX(SPLIT({value_of("screening_questions")},CHAR(10)),'
        f'1,{number}),"")'
    )


def screening_label(row: int, number: int) -> str:
    """Номер вопроса — только если вопрос есть.

    Строк отведено столько, сколько конвейер может записать в худшем случае,
    а обычно их на одну меньше. Подпись «Вопрос 4» над пустотой сбивала бы с
    толку: искали бы вопрос, которого не задавали.
    """
    return cell(f'IF($B{row}="","",{lit(f"Вопрос {number}")})')


# --- Сборка листа ---------------------------------------------------------

def card_values() -> tuple[list[list[str]], list[int]]:
    """Содержимое карточки и номера строк-заголовков (с единицы)."""
    rows: list[list[str]] = []
    bold: list[int] = []

    def add(a: str = "", b: str = "", c: str = "", *, head: bool = False) -> int:
        rows.append([a, b, c])
        if head:
            bold.append(len(rows))
        return len(rows)

    for row in top_rows():
        add(*row)
    add()

    add(STOPS_TITLE, head=True)
    for question in stop_questions():
        field = question["field"]
        row = add(question["text"], cell(value_of(field)))
        rows[row - 1][2] = stop_mark(row, field)
    add()

    add(FLAGS_TITLE, head=True)
    for question in flag_questions():
        add(question["text"], flag_answer(question), flag_mark(question["field"]))
    add()

    add(ANSWERS_TITLE, head=True)
    for question in open_questions():
        field = question["field"]
        add(question["text"], cell(value_of(field)), open_score(field))
    add()

    add(SCREENING_TITLE, head=True)
    for number in range(1, SCREENING_ROWS + 1):
        row = add("", screening_answer(number))
        rows[row - 1][0] = screening_label(row, number)
    add(CONCERNS_LABEL, cell(value_of("flags_ru")))

    return rows, bold


def column_widths() -> list[int]:
    """Ширина колонок в пикселях."""
    return [chars * PIXELS_PER_CHAR for chars in COLUMN_CHARS]


def name_source() -> str:
    """Источник выпадающего списка: колонка fio, со второй строки и до конца.

    Диапазон открытый — карточка не должна упираться в разметку листа, когда
    анкет станет больше трёхсот. Пустые ячейки Sheets в список не берёт,
    поэтому отдельно отсеивать их не нужно.
    """
    fio = col_letter(index_of("fio"))
    return f"='{WORKSHEET_NAME}'!${fio}${FIRST_DATA_ROW}:${fio}"


def export_url(gid: int, rows: int) -> str:
    """Ссылка на PDF: книжная ориентация, вписано в страницу, только карточка.

    Заменяет собой настройки печати, которых в Sheets API нет.
    """
    return (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=pdf"
        f"&gid={gid}&portrait=true&scale=4&gridlines=false&sheetnames=false"
        f"&printtitle=false&range=A1:{col_letter(CARD_COLS)}{rows}"
    )


# ------------------------------ работа с книгой ---------------------------

def clear_header_notes(data: gspread.Worksheet) -> None:
    """Стирает примечания во всей шапке «Данных».

    Их писала прежняя затея. Пустой текст в update_notes и есть удаление —
    отдельного запроса на это в API нет.
    """
    notes = {f"{col_letter(i)}1": "" for i in range(1, len(HEADERS) + 1)}
    data.update_notes(notes)
    log.info("примечания к шапке «%s» сняты: %d колонок", WORKSHEET_NAME, len(notes))


def drop_old_sheet(book: gspread.Spreadsheet) -> None:
    """Удаляет лист «Вопросы». Нет его — и хорошо, дважды удалять нечего."""
    try:
        book.del_worksheet(book.worksheet(OLD_SHEET_TITLE))
    except gspread.WorksheetNotFound:
        return
    log.info("лист «%s» удалён", OLD_SHEET_TITLE)


def open_card(book: gspread.Spreadsheet) -> gspread.Worksheet:
    """Лист «Карточка»: существующий или новый. Повторный запуск не плодит второй."""
    try:
        return book.worksheet(CARD_TITLE)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=CARD_TITLE, rows=2, cols=CARD_COLS)
        log.info("создан лист «%s»", CARD_TITLE)
        return ws


def selected_name(ws: gspread.Worksheet) -> str:
    """Фамилия, выбранная в карточке сейчас. Пересборка её сохраняет."""
    try:
        return (ws.row_values(1) or [""])[0].strip()
    except Exception:
        log.exception("не удалось прочитать выбранную фамилию — карточка соберётся пустой")
        return ""


def write_card(ws: gspread.Worksheet, values: list[list[str]], bold: list[int]) -> None:
    """Пишет карточку заново. Лишние строки не остаются: лист ужимается.

    Формулы записываются USER_ENTERED — единственный способ положить в ячейку
    формулу, а не её текст.
    """
    ws.resize(rows=max(len(values), 2), cols=CARD_COLS)
    last = f"{col_letter(CARD_COLS)}{len(values)}"
    ws.update(values=values, range_name=f"A1:{last}", value_input_option="USER_ENTERED")
    add_name_dropdown(ws)
    decorate(ws, len(values), bold)


def add_name_dropdown(ws: gspread.Worksheet) -> None:
    """Выпадающий список фамилий в A1.

    Проверка нестрогая: список подсказывает, но не запрещает стереть ячейку
    или вписать фамилию руками — на пустой карточке это единственный способ
    начать.
    """
    try:
        ws.add_validation(
            NAME_A1,
            ValidationConditionType.one_of_range,
            [name_source()],
            showCustomUi=True,
            strict=False,
        )
    except Exception:
        log.exception("выпадающий список фамилий не поставлен — задайте руками")


def decorate(ws: gspread.Worksheet, rows: int, bold: list[int]) -> None:
    """Перенос текста, выравнивание по верху, жирные заголовки, ширина колонок.

    Оформление — не содержание: если Sheets откажет, карточка уже читается,
    и ронять из-за этого весь прогон незачем.
    """
    body = f"A1:{col_letter(CARD_COLS)}{rows}"
    try:
        ws.batch_format(
            [
                {
                    "range": body,
                    "format": {
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "TOP",
                        "textFormat": {"bold": False},
                    },
                },
                *[
                    {
                        "range": f"A{i}:{col_letter(CARD_COLS)}{i}",
                        "format": {"textFormat": {"bold": True}},
                    }
                    for i in bold
                ],
            ]
        )
        # Высота строки считается по ширине колонок, поэтому сначала ширина.
        ws.spreadsheet.batch_update(
            {
                "requests": [
                    *[
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": ws.id,
                                    "dimension": "COLUMNS",
                                    "startIndex": i,
                                    "endIndex": i + 1,
                                },
                                "properties": {"pixelSize": width},
                                "fields": "pixelSize",
                            }
                        }
                        for i, width in enumerate(column_widths())
                    ],
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": ws.id,
                                "dimension": "ROWS",
                                "startIndex": 0,
                                "endIndex": rows,
                            }
                        }
                    },
                ]
            }
        )
    except Exception:
        log.exception("оформление листа «%s» не удалось — содержимое на месте", CARD_TITLE)


def set_visibility(book: gspread.Spreadsheet) -> None:
    """Склад прячем, рабочие листы показываем.

    Отсутствующий лист — не ошибка: «Кандидаты» и «Как читать» собраны в
    книге руками, и в чужой копии их может не быть вовсе.
    """
    wanted = {title: True for title in HIDDEN_SHEETS}
    wanted.update({title: False for title in VISIBLE_SHEETS})

    by_title = {ws.title: ws for ws in book.worksheets()}
    requests = []
    for title, hidden in wanted.items():
        ws = by_title.get(title)
        if ws is None:
            log.warning("листа «%s» в книге нет — видимость не трогаю", title)
            continue
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "hidden": hidden},
                    "fields": "hidden",
                }
            }
        )

    if requests:
        book.batch_update({"requests": requests})
    log.info(
        "видимость листов: скрыты %s, показаны %s",
        ", ".join(HIDDEN_SHEETS), ", ".join(VISIBLE_SHEETS),
    )


def sync() -> str:
    """Пересобирает карточку и приводит книгу в порядок. Отдаёт ссылку на PDF."""
    data = sheets.worksheet()
    book = data.spreadsheet

    clear_header_notes(data)
    drop_old_sheet(book)

    card = open_card(book)
    name = selected_name(card)
    values, bold = card_values()
    values[0][0] = name
    write_card(card, values, bold)
    log.info(
        "лист «%s» собран: %d строк, выбрано «%s»", CARD_TITLE, len(values), name or "—"
    )

    set_visibility(book)
    return export_url(card.id, len(values))


def dry_run() -> None:
    """Печатает карточку в терминал. В книгу не ходит."""
    values, bold = card_values()
    print(f"\n=== лист «{CARD_TITLE}» ({len(values)} строк) ===", flush=True)
    for i, row in enumerate(values, start=1):
        mark = "**" if i in bold else "  "
        print(f"{i:>3}{mark} " + " | ".join(row), flush=True)

    print(
        f"\n=== книга ===\n"
        f"удалить лист «{OLD_SHEET_TITLE}», снять примечания со всей шапки "
        f"«{WORKSHEET_NAME}»\n"
        f"скрыть: {', '.join(HIDDEN_SHEETS)}\n"
        f"показать: {', '.join(VISIBLE_SHEETS)}\n"
        f"ширина колонок, пикселей: {', '.join(str(w) for w in column_widths())}\n",
        flush=True,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Пересобрать лист «Карточка» и видимость листов книги"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="напечатать карточку в терминал"
    )
    args = parser.parse_args(argv)

    setup_logging()

    # Сухой прогон собирается целиком из questions.json и в книгу не ходит,
    # поэтому и ключей не требует: карточку можно посмотреть на месте, правя
    # формулировки.
    if args.dry_run:
        dry_run()
        log.info("сухой прогон: книга не тронута")
        return 0

    missing = [s for s in missing_secrets() if s != "ANTHROPIC_API_KEY"]
    if missing:
        log.error("не заданы переменные окружения: %s", ", ".join(missing))
        return 2

    try:
        link = sync()
    except Exception:
        log.exception("не удалось собрать карточку")
        return 1

    print(
        f"\nГотово. Лист «{CARD_TITLE}» собран из questions.json.\n"
        f"Печать одной страницей (книжная ориентация задана в ссылке):\n{link}\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
