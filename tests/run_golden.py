#!/usr/bin/env python3
"""Эталонный прогон: фикстуры через реальный API и таблица расхождений.

Отдельно от pytest намеренно — здесь настоящие вызовы модели, то есть
деньги и минуты. В CI гоняются детерминированные тесты, а этот скрипт
запускается руками, когда меняются промпт или рубрики.

    ANTHROPIC_API_KEY=... .venv/bin/python tests/run_golden.py
    .venv/bin/python tests/run_golden.py 01_strong_a 04_pasted_downgrade
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ANTHROPIC_API_KEY, SCORER_MODEL, setup_logging  # noqa: E402
from app.models import QKEYS  # noqa: E402
from app.scorer import ScoringFailed, score  # noqa: E402
from app.verdict import decide  # noqa: E402
from tests.conftest import load_fixtures  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def cell(expected, actual):
    if actual is None:
        return f"{DIM}  — {RESET}"
    delta = actual - expected
    if delta == 0:
        return f"{GREEN}{expected}={actual}{RESET}"
    colour = YELLOW if abs(delta) == 1 else RED
    return f"{colour}{expected}→{actual}{RESET}"


def main(argv: list[str]) -> int:
    setup_logging()
    # Таблица должна читаться — INFO-строки конвейера здесь только мешают.
    logging.getLogger().setLevel(logging.WARNING)
    if not ANTHROPIC_API_KEY:
        print("Не задан ANTHROPIC_API_KEY — прогон невозможен.", file=sys.stderr)
        return 2

    fixtures = load_fixtures()
    if argv:
        wanted = {a.replace(".json", "") for a in argv}
        fixtures = [f for f in fixtures if f["_file"].replace(".json", "") in wanted]
        if not fixtures:
            print(f"Фикстуры не найдены: {', '.join(argv)}", file=sys.stderr)
            return 2

    print(f"\nМодель: {SCORER_MODEL}   Фикстур: {len(fixtures)}\n")
    header = f"{'фикстура':<26}" + "".join(f"{q:>12}" for q in QKEYS) + f"{'сумма':>14}{'вердикт':>16}"
    print(header)
    print("─" * 104)

    deltas: list[int] = []
    exact_rows = 0
    verdict_hits = 0
    scored_rows = 0
    failures: list[str] = []

    for f in fixtures:
        name = f["_file"].replace(".json", "")
        exp = f["expected"]

        if exp.get("scores") is None:
            print(f"{name:<26}{DIM}{'ранний выход по стоп-вопросам':>76}{RESET}")
            continue

        try:
            result, zeroed = score(f["answers"])
        except ScoringFailed as e:
            print(f"{name:<26}{RED}{('ошибка: ' + str(e)[:60]):>76}{RESET}")
            failures.append(name)
            continue

        scored_rows += 1
        row_deltas = []
        cells = []
        for q in QKEYS:
            want, got = exp["scores"][q], result.scores[q]
            row_deltas.append(got - want)
            cells.append(f"{cell(want, got):>21}")
        deltas.extend(row_deltas)
        if all(d == 0 for d in row_deltas):
            exact_rows += 1

        v = decide(
            scores=result.scores,
            flags=exp["flags"],
            ai_style=result.ai_style,
            q3_senior_dm_confirmed=result.auto_filters.q3_senior_dm_confirmed,
        )
        if v.verdict == exp["verdict"]:
            verdict_hits += 1
            verdict_cell = f"{GREEN}{v.verdict}={exp['verdict']}{RESET}"
        else:
            verdict_cell = f"{RED}{exp['verdict']}→{v.verdict}{RESET}"

        total_cell = cell(exp["total"], v.total)
        print(f"{name:<26}" + "".join(cells) + f"{total_cell:>23}{verdict_cell:>25}")

        if zeroed:
            print(f"{'':<26}{DIM}обнулено без цитаты: {'; '.join(zeroed)}{RESET}")
        if result.ai_style > 70:
            print(f"{'':<26}{DIM}стиль ИИ {result.ai_style}/100: {', '.join(result.ai_markers)}{RESET}")

    print("─" * 104)
    if deltas:
        mae = sum(abs(d) for d in deltas) / len(deltas)
        within_one = sum(1 for d in deltas if abs(d) <= 1) / len(deltas) * 100
        print(
            f"\nБаллов сверено: {len(deltas)}   "
            f"средняя ошибка: {mae:.2f}   "
            f"расхождение не больше 1: {within_one:.0f}%"
        )
        print(
            f"Полное совпадение строк: {exact_rows}/{scored_rows}   "
            f"вердикт угадан: {verdict_hits}/{scored_rows}"
        )
    if failures:
        print(f"{RED}Не отработали: {', '.join(failures)}{RESET}")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
