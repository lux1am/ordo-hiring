#!/usr/bin/env python3
"""Запись шапки листа «Данные» из HEADERS.py.

Порядок колонок задан в HEADERS.py, но физически живёт в таблице, и при
смене схемы анкеты эти два места расходятся. Раньше шапку правили руками —
54 имени, набранные в строку, ошибиться в которых проще, чем не ошибиться.
Этот скрипт записывает её из того же файла, по которому потом сверяется.

Запускается вторым, после archive_old.py:

    python3 archive_old.py     # сначала: увезти старые строки в архив
    python3 apply_headers.py   # потом: записать новую шапку

Порядок обязателен, и скрипт его сторожит: под старыми строками менять
шапку нельзя — колонка сменит имя, а данные под ней останутся прежними, и
строка начнёт означать не то, что в ней написано. Если строки на месте,
скрипт отказывается работать и отсылает к archive_old.py; обойти проверку —
--force, но тогда за смысл данных отвечаете вы.

Одно исключение: колонка, дописанная в конец шапки, разрешена и при
непустом листе. Ни одно прежнее имя при этом не переезжает, и анкеты под
ними сохраняют смысл — архивировать живых людей ради новой колонки
незачем. Так в лист добавилась метка размещения (BC).

Запуск:

    python3 apply_headers.py            # записать шапку
    python3 apply_headers.py --dry-run  # только показать расхождения
    python3 apply_headers.py --force    # записать поверх, не глядя на строки

После шапки скрипт сам пересобирает лист «Карточка», см. sync_questions.py:
имена колонок и формулировки вопросов меняются одним и тем же движением, и
оставлять карточку на отдельный запуск значит рано или поздно про неё
забыть. Там же приводится в порядок видимость листов книги.

Код возврата: 0 — шапка на месте, 1 — не записана (есть строки или сбой
таблицы), 2 — не настроено окружение. Сбой карточки кода не меняет: шапка к
этому моменту записана и проверена, а карточка пересобирается отдельным
запуском sync_questions.py.
"""
import argparse
import logging
import sys

import sync_questions
from HEADERS import FIRST_DATA_ROW, HEADERS, a1_range, col_letter
from app import sheets
from app.config import WORKSHEET_NAME, missing_secrets, setup_logging

log = logging.getLogger("apply_headers")

HEADER_ROW = 1
HEADER_RANGE = a1_range(HEADER_ROW, 1, len(HEADERS))


# --------------------------- чистые функции -------------------------------

def header_diff(actual: list[str]) -> list[tuple[int, str, str]]:
    """Расхождения шапки с HEADERS.py: (номер колонки, ожидалось, в таблице)."""
    padded = list(actual) + [""] * len(HEADERS)
    return [
        (i, want, str(padded[i - 1]).strip())
        for i, want in enumerate(HEADERS, start=1)
        if str(padded[i - 1]).strip() != want
    ]


def occupied_rows(values: list[list]) -> int:
    """Сколько строк с данными лежит под шапкой.

    Заполненность считает sheets.row_has_data — размеченная вперёд формула
    в AN данными не считается, иначе скрипт отказывался бы работать на
    заведомо пустом листе.
    """
    return sum(1 for row in values[FIRST_DATA_ROW - 1:] if sheets.row_has_data(row))


def appended_only(diff: list[tuple[int, str, str]], actual: list[str]) -> bool:
    """Расхождение — только новые колонки, дописанные в конец пустой шапки?

    Такое бывает ровно в одном случае: в раскладку добавили колонку, и
    физически она ещё не заведена. Старые строки от этого не меняют смысла —
    ни одно имя не переехало, — поэтому архивировать анкеты живых людей
    ради такой правки незачем, и шапка пишется поверх строк.

    Всё остальное — переименование, перестановка, другой состав — по-прежнему
    требует архива: там ячейка под новым именем означала бы не то, что в ней
    написано.
    """
    width = len([cell for cell in actual if str(cell).strip()])
    if not diff or not width:
        # Пустая шапка — не «дописали в конец», а несобранный лист: что
        # означают строки под ней, неизвестно, и решать это должен человек.
        return False
    return all(i > width and not got for i, _, got in diff)


def describe(diff: list[tuple[int, str, str]]) -> list[str]:
    return [
        f"  {col_letter(i)} ({i}): «{got or '—'}» → «{want}»" for i, want, got in diff
    ]


def rebuild_card(dry_run: bool) -> None:
    """Лист «Карточка» и видимость листов, см. sync_questions.py.

    Своего кода возврата не имеет намеренно. Шапка к этому моменту записана
    и перечитана — объявить прогон неудачным из-за карточки значило бы
    позвать запустить всё заново там, где хватит одного sync_questions.py.
    """
    if dry_run:
        log.info("сухой прогон: карточка не пересобирается")
        return
    try:
        sync_questions.sync()
    except Exception:
        log.exception(
            "шапка записана, но карточка не пересобрана — "
            "запустите отдельно: python3 sync_questions.py"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Записать шапку листа «Данные» из HEADERS.py"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="только показать расхождения"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="записать даже при непустом листе (данные останутся под новыми именами)",
    )
    args = parser.parse_args(argv)

    setup_logging()
    missing = [s for s in missing_secrets() if s != "ANTHROPIC_API_KEY"]
    if missing:
        log.error("не заданы переменные окружения: %s", ", ".join(missing))
        return 2

    try:
        ws = sheets.worksheet()
        values = ws.get_all_values()
    except Exception:
        log.exception("не удалось прочитать лист «%s»", WORKSHEET_NAME)
        return 1

    diff = header_diff(values[0] if values else [])
    if not diff:
        # Формулировки вопросов могли поменяться и без правки имён колонок,
        # да и видимость листов слетает сама: карточку собираем в любом случае.
        log.info("шапка уже совпадает с HEADERS.py (%d колонок)", len(HEADERS))
        rebuild_card(args.dry_run)
        return 0

    log.info("расхождений с HEADERS.py: %d", len(diff))
    for line in describe(diff):
        print(line, flush=True)

    rows = occupied_rows(values)
    appended = appended_only(diff, values[0] if values else [])
    if rows and appended:
        log.info(
            "расхождение — только дописанные в конец колонки (%s); имена "
            "прежних колонок не менялись, строки под ними сохраняют смысл",
            ", ".join(col_letter(i) for i, _, _ in diff),
        )
    elif rows and not args.force:
        log.error(
            "в листе «%s» осталось строк с данными: %d. Под новой шапкой они "
            "означали бы не то, что в них написано. Сначала уберите их: "
            "python3 archive_old.py",
            WORKSHEET_NAME, rows,
        )
        return 1
    elif rows:
        log.warning("--force: шапка меняется поверх %d строк с данными", rows)

    if args.dry_run:
        log.info("сухой прогон: шапка не записана")
        return 0

    try:
        ws.update(values=[HEADERS], range_name=HEADER_RANGE, value_input_option="RAW")
        # Перечитываем, а не верим ответу API: расхождение шапки означает,
        # что баллы попадут не в те ячейки, и узнать об этом лучше сейчас.
        written = header_diff(ws.row_values(HEADER_ROW))
    except Exception:
        log.exception("не удалось записать шапку листа «%s»", WORKSHEET_NAME)
        return 1

    if written:
        log.error("шапка записана, но всё ещё расходится:")
        for line in describe(written):
            print(line, flush=True)
        return 1

    log.info("шапка листа «%s» записана: %d колонок", WORKSHEET_NAME, len(HEADERS))
    rebuild_card(args.dry_run)
    print(f"\nГотово. Диапазон {HEADER_RANGE} совпадает с HEADERS.py.\n", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
