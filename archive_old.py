#!/usr/bin/env python3
"""Разовый перенос анкет прежней схемы в отдельный лист.

Нужен ровно один раз — при смене схемы анкеты, когда меняется состав или
смысл колонок. Старые строки нельзя оставить в «Данных»: под новой шапкой
они означали бы не то, что в них написано, — колонка «часы в неделю»
показывала бы ответ про комиссию. И удалять их нельзя: это анкеты живых
людей. Поэтому они уезжают в отдельный лист целиком, вместе со своей
шапкой, а «Данные» остаются пустыми под новую схему.

Порядок при смене схемы:

    python3 archive_old.py     # сначала: увезти старые строки
    python3 apply_headers.py   # потом: записать новую шапку

Скрипт идемпотентный: строки узнаются по id из колонки A, поэтому повторный
запуск ничего не дублирует, а прерванный на середине — доводит дело до
конца. Лист «Архив» переиспользуется, если уже создан.

Формула total_score в AN не трогается: она размечена в таблице на 300 строк
вперёд и к данным отношения не имеет. Всё остальное в строках 2 и ниже
очищается.

Запуск:

    python3 archive_old.py            # перенести и очистить
    python3 archive_old.py --dry-run  # только показать, что будет сделано

Код возврата: 0 — готово (в том числе когда переносить было нечего),
1 — ошибка при работе с таблицей, 2 — не настроено окружение.
"""
import argparse
import logging
import sys

import gspread

from HEADERS import (
    COL_FORM_START,
    COL_SOURCE,
    COL_RESULT_START,
    COL_SCORES_END,
    COL_TOTAL_SCORE,
    FIRST_DATA_ROW,
    HEADERS,
    col_letter,
)
from app import sheets
from app.config import WORKSHEET_NAME, missing_secrets, setup_logging

log = logging.getLogger("archive_old")

ARCHIVE_TITLE = "Архив (старая анкета)"

# Ширина архивного листа. Прежняя раскладка могла быть у́же нынешней, но
# сужать лист незачем: пустые колонки места не занимают, а не влезшие
# данные потерялись бы молча.
ARCHIVE_MIN_COLS = len(HEADERS)


# --------------------------- чистые функции -------------------------------
#
# Всё, что можно посчитать без сети, посчитано здесь: тогда идемпотентность
# проверяется тестом, а не осторожным повторным запуском на боевой таблице.


def data_rows(values: list[list]) -> list[list]:
    """Строки с данными: всё ниже шапки, где заполнено хоть что-то.

    Заполненность считает sheets.row_has_data — формула в AN не в счёт:
    без этого в архив уехали бы три сотни пустышек, размеченных формулой
    вперёд.
    """
    return [row for row in values[FIRST_DATA_ROW - 1:] if sheets.row_has_data(row)]


def row_key(row: list):
    """Чем строка узнаётся при повторном запуске.

    Обычно это id из колонки A — он уникален и не меняется. Если id пуст
    (строку правили руками, запись оборвалась на середине), ключом
    становится вся строка целиком: лучше сравнить дороже, чем задвоить.
    """
    first = str(row[0]).strip() if row else ""
    return first or tuple(str(c) for c in row)


def rows_to_append(archive: list[list], source: list[list]) -> list[list]:
    """Строки, которых в архиве ещё нет.

    Сравнение идёт по ключу строки, а не по количеству: прерванный на
    середине прогон мог успеть перенести часть, и дописывать нужно ровно
    остаток.
    """
    known = {row_key(row) for row in data_rows(archive)}
    out = []
    for row in source:
        key = row_key(row)
        if key in known:
            continue
        known.add(key)
        out.append(row)
    return out


def clear_ranges(last_row: int) -> list[str]:
    """Диапазоны очистки «Данных»: всё ниже шапки, кроме колонки AN.

    AN пропускается намеренно — там формула total_score, размеченная в
    таблице заранее. Стереть её значит оставить сумму баллов пустой у всех
    будущих анкет.
    """
    if last_row < FIRST_DATA_ROW:
        return []
    left = f"{col_letter(COL_FORM_START)}{FIRST_DATA_ROW}:{col_letter(COL_SCORES_END)}{last_row}"
    # Правый диапазон идёт до последней колонки листа, а не до конца зоны
    # результата: за ней стоят заметки, исход и метка размещения, и
    # оставленная метка приписала бы старый отклик будущему размещению.
    right = f"{col_letter(COL_RESULT_START)}{FIRST_DATA_ROW}:{col_letter(COL_SOURCE)}{last_row}"
    assert COL_SCORES_END < COL_TOTAL_SCORE < COL_RESULT_START <= COL_SOURCE
    return [left, right]


# ------------------------------ работа с книгой ---------------------------

def open_archive(book: gspread.Spreadsheet, cols: int) -> gspread.Worksheet:
    """Лист архива: существующий или новый. Повторный запуск не создаёт второй."""
    try:
        ws = book.worksheet(ARCHIVE_TITLE)
        log.info("лист «%s» уже есть, дописываю в него", ARCHIVE_TITLE)
        return ws
    except gspread.WorksheetNotFound:
        pass
    ws = book.add_worksheet(title=ARCHIVE_TITLE, rows=1, cols=max(cols, ARCHIVE_MIN_COLS))
    log.info("создан лист «%s»", ARCHIVE_TITLE)
    return ws


def hide(ws: gspread.Worksheet) -> None:
    """Архив прячется с глаз: смотреть в него будут редко и по делу.

    Неудача здесь не повод считать перенос несостоявшимся — данные уже на
    месте, а видимость листа рекрутёр поправит мышкой.
    """
    try:
        ws.hide()
    except Exception:
        log.exception("не удалось скрыть лист «%s» — скройте вручную", ARCHIVE_TITLE)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Разовый перенос анкет прежней схемы в лист «Архив»"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="только показать, что будет сделано"
    )
    args = parser.parse_args(argv)

    setup_logging()
    missing = [s for s in missing_secrets() if s != "ANTHROPIC_API_KEY"]
    if missing:
        log.error("не заданы переменные окружения: %s", ", ".join(missing))
        return 2

    try:
        source = sheets.worksheet()
        values = source.get_all_values()
    except Exception:
        log.exception("не удалось прочитать лист «%s»", WORKSHEET_NAME)
        return 1

    header = values[0] if values else []
    rows = data_rows(values)

    if header and header[: len(HEADERS)] == HEADERS and rows:
        log.warning(
            "шапка листа «%s» уже совпадает с HEADERS.py, а строки ещё старые — "
            "похоже, apply_headers.py запустили раньше времени. В архив уедет "
            "новая шапка над старыми данными",
            WORKSHEET_NAME,
        )

    if args.dry_run:
        log.info(
            "сухой прогон: строк к переносу %d, очистка диапазонов %s",
            len(rows), ", ".join(clear_ranges(source.row_count)) or "не требуется",
        )
        return 0

    if not rows:
        # Пустого архива не заводим: повторный запуск после удачного
        # переноса не должен оставлять за собой лишний лист.
        log.info("в листе «%s» нет строк с данными — переносить нечего", WORKSHEET_NAME)
        return 0

    try:
        archive = open_archive(source.spreadsheet, len(header))
        existing = archive.get_all_values()

        # Шапка пишется только в пустой архив: у заполненного она своя и
        # менять её нельзя — под ней лежат данные прежней раскладки.
        if not existing and header:
            archive.update(values=[header], range_name="A1", value_input_option="RAW")
            existing = [header]

        pending = rows_to_append(existing, rows)
        if pending:
            archive.append_rows(pending, value_input_option="RAW")
        log.info(
            "в архив перенесено строк: %d (было там %d)",
            len(pending), len(data_rows(existing)),
        )
        hide(archive)

        ranges = clear_ranges(source.row_count)
        if ranges:
            source.batch_clear(ranges)
        log.info(
            "лист «%s» очищен: осталась шапка и формулы в %s",
            WORKSHEET_NAME, col_letter(COL_TOTAL_SCORE),
        )
    except Exception:
        # Обещать «ничего не тронуто» нельзя: упасть могло и после переноса,
        # на очистке. Зато повтор безопасен — строки узнаются по id.
        log.exception(
            "перенос прерван. Запустите скрипт ещё раз: он допишет остаток и "
            "очистит «%s», ничего не задвоив",
            WORKSHEET_NAME,
        )
        return 1

    print(
        f"\nГотово. Строк в архиве: {len(data_rows(existing)) + len(pending)}.\n"
        f"Следующий шаг — записать новую шапку: python3 apply_headers.py\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
