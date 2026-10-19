#!/usr/bin/env python3
"""Обработка анкет со статусом new.

Нужен варианту с Apps Script: там анкету принимает doPost, веб-сервера нет,
и запускать BackgroundTasks некому — строки просто копятся со статусом new.
Этот скрипт подбирает их и прогоняет через тот же конвейер.

Запускается по расписанию из GitHub Actions (.github/workflows/worker.yml).
Вручную:

    python3 process_new.py            # обработать все new
    python3 process_new.py --dry-run  # только показать, что будет обработано
    python3 process_new.py --limit 5  # не больше пяти за запуск

Идемпотентность обеспечивает сам конвейер: он берёт строку только из new и
сразу переводит её в processing. Два одновременных запуска не обработают
одну строку дважды.

В конце прогона пересобирается «Сводка для Поли» — выгрузка для внешнего
бота, см. digest.py. Делается это всегда, а не только когда были новые
анкеты: окно последних дней в сводке сдвигается само.

Код возврата: 0 — всё в порядке (строки в manual ошибкой не считаются),
1 — часть строк упала с исключением, 2 — не настроено окружение.
"""
import argparse
import logging
import os
import sys
from collections import Counter

import digest
from app import recovery, sheets
from app.config import (
    STATUS_DONE,
    STATUS_MANUAL,
    STATUS_NEW,
    missing_secrets,
    setup_logging,
)
from app.pipeline import (
    OUTCOME_BUSY,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    process_row,
)

log = logging.getLogger("process_new")

LABELS = [
    (STATUS_DONE, "оценено"),
    (STATUS_MANUAL, "в ручную проверку"),
    (OUTCOME_SKIPPED, "пропущено (статус изменился)"),
    (OUTCOME_BUSY, "занято другим потоком"),
    (OUTCOME_FAILED, "упало с ошибкой"),
]


def print_summary(revived: int, outcomes: Counter) -> None:
    """Сводка прогона.

    Панели наблюдения нет, и логи GitHub Actions — единственное окно в
    работу конвейера. Поэтому сводка печатается всегда, даже когда делать
    было нечего, и оформлена так, чтобы читаться с одного взгляда.
    """
    processed = sum(outcomes[k] for k, _ in LABELS)
    lines = [
        "",
        "═" * 46,
        "СВОДКА ПРОГОНА",
        "═" * 46,
        f"  зависших строк возвращено : {revived}",
        f"  строк обработано          : {processed}",
    ]
    for key, label in LABELS:
        if outcomes[key]:
            lines.append(f"      {label:<30}: {outcomes[key]}")
    lines.append("═" * 46)
    for line in lines:
        print(line, flush=True)

    _write_step_summary(revived, processed, outcomes)


def _write_step_summary(revived: int, processed: int, outcomes: Counter) -> None:
    """Та же сводка на страницу прогона GitHub Actions, если мы внутри него.

    Step Summary виден сразу, без разворачивания логов — при запуске раз в
    десять минут это заметно экономит время.
    """
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = [f"| зависших возвращено | {revived} |", f"| обработано | {processed} |"]
    rows += [f"| {label} | {outcomes[key]} |" for key, label in LABELS if outcomes[key]]
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("### Обработка анкет\n\n| | |\n|---|---:|\n" + "\n".join(rows) + "\n")
    except OSError as e:
        log.warning("не удалось записать сводку в GITHUB_STEP_SUMMARY: %s", e)


def refresh_digest() -> None:
    """Пересобрать «Сводку для Поли» — выгрузку для внешнего бота.

    Делается в конце каждого прогона, даже когда новых анкет не было: в
    сводке есть окно последних дней, оно сдвигается само, а размещения и
    колонку «Что было дальше» Ника заполняет между прогонами.

    Сбой здесь прогон не роняет и красным его не делает: анкеты оценены,
    это главное. В сводке останутся числа прошлого прогона, и в логе
    сказано, почему.
    """
    try:
        digest.refresh()
    except Exception:
        log.exception("сводка для бота не обновлена — в ней числа прошлого прогона")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Обработка анкет со статусом new")
    parser.add_argument("--dry-run", action="store_true", help="только показать список")
    parser.add_argument("--limit", type=int, default=0, help="максимум строк за запуск")
    parser.add_argument(
        "--skip-recovery", action="store_true",
        help="не возвращать зависшие строки перед прогоном",
    )
    args = parser.parse_args(argv)

    setup_logging()
    missing = missing_secrets()
    if missing:
        log.error("не заданы переменные окружения: %s", ", ".join(missing))
        return 2

    # Зависшие строки подбираем здесь же: отдельного процесса со сканером в
    # этом варианте нет. Каждый прогон — новый процесс, поэтому все строки
    # в processing по определению остались от прерванного прогона.
    revived = 0
    if not args.skip_recovery and not args.dry_run:
        try:
            revived = len(recovery.recover(startup=True))
        except Exception:
            log.exception("не удалось вернуть зависшие строки, продолжаю")

    try:
        rows = sheets.rows_with_status(STATUS_NEW)
    except Exception:
        log.exception("не удалось прочитать лист")
        return 1

    if args.limit:
        rows = rows[: args.limit]

    outcomes: Counter = Counter()
    if rows:
        log.info("к обработке строк: %d (%s)", len(rows), ", ".join(map(str, rows)))
        if args.dry_run:
            return 0
        for row in rows:
            outcomes[process_row(row)] += 1
    else:
        log.info("анкет со статусом %s нет", STATUS_NEW)
        if args.dry_run:
            return 0

    refresh_digest()
    print_summary(revived, outcomes)

    if outcomes[OUTCOME_FAILED]:
        # Красный прогон здесь — полезный сигнал: строки остались в
        # processing, следующий запуск их подберёт, но причину стоит увидеть.
        log.error("строк упало с ошибкой: %d", outcomes[OUTCOME_FAILED])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
