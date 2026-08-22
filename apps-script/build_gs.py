#!/usr/bin/env python3
"""Генерация Apps Script из источников репозитория.

Порядок колонок и код телеметрии не должны существовать в двух вариантах —
разойдясь, они начнут писать баллы не в те ячейки и молча ломать проверки.
Поэтому Headers.gs, Questions.gs и Index.html собираются автоматически:

    HEADERS.py           -> apps-script/Headers.gs
    questions.json       -> apps-script/Questions.gs
    templates/form.html  -> apps-script/Index.html   (CSS и JS дословно)
      + templates/apps-script-index.html — заготовка разметки

Заготовка лежит в templates/, а не рядом с результатом: всё, что попадает
в apps-script/, clasp считает частью проекта, и файл с плейсхолдерами
уехал бы в Apps Script лишним.

Запуск:  python3 apps-script/build_gs.py
Проверка актуальности:  python3 apps-script/build_gs.py --check
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "apps-script"
sys.path.insert(0, str(ROOT))

import HEADERS as H  # noqa: E402

BANNER = "// СГЕНЕРИРОВАНО apps-script/build_gs.py — не редактировать вручную.\n"


def extract(tag: str, html: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", html, re.S)
    if not m:
        raise SystemExit(f"в templates/form.html не найден блок <{tag}>")
    return m.group(1).strip("\n")


def build_headers() -> str:
    names = ",\n  ".join(json.dumps(n, ensure_ascii=False) for n in H.HEADERS)
    return (
        f"{BANNER}// Источник: HEADERS.py\n\n"
        f"const HEADERS = [\n  {names}\n];\n\n"
        f"const COL_FORM_END = {H.COL_FORM_END};        // AH — последняя колонка анкеты\n"
        f"const COL_FLAGS = {H.index_of('flags')};             // AS\n"
        f"const COL_STATUS = {H.index_of('status')};            // AU\n"
        f"const COL_TELEGRAM = {H.index_of('telegram')};           // D\n"
        f"const COL_TOTAL_SCORE = {H.COL_TOTAL_SCORE};        // AN — формула, не трогаем\n"
        f"const FIRST_DATA_ROW = {H.FIRST_DATA_ROW};\n"
        f"const LAST_PREPARED_ROW = {H.LAST_PREPARED_ROW};\n"
        f"const FORMULA_TOPUP_FROM_ROW = {H.FORMULA_TOPUP_FROM_ROW};\n"
        f"const TOTAL_SCORE_FORMULA = {json.dumps(H.TOTAL_SCORE_FORMULA, ensure_ascii=False)};\n"
    )


def build_questions() -> str:
    data = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return f"{BANNER}// Источник: questions.json\n\nconst QUESTIONS = {body};\n"


def build_index() -> str:
    form = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")
    template = (ROOT / "templates" / "apps-script-index.html").read_text(encoding="utf-8")
    # CSS и JS переносятся дословно: телеметрия должна вести себя одинаково
    # в обоих вариантах формы.
    out = template.replace("/*__CSS__*/", extract("style", form))
    out = out.replace("//__JS__", extract("script", form))
    return out


TARGETS = {
    "Headers.gs": build_headers,
    "Questions.gs": build_questions,
    "Index.html": build_index,
}


def warn_if_clasp_unconfigured() -> None:
    """Пустой scriptId — самая частая причина непонятной ошибки при push."""
    config = ROOT / ".clasp.json"
    if not config.exists():
        return
    try:
        script_id = json.loads(config.read_text(encoding="utf-8")).get("scriptId", "")
    except json.JSONDecodeError:
        print("ВНИМАНИЕ: .clasp.json не разбирается как JSON", file=sys.stderr)
        return
    if not script_id:
        print(
            "ВНИМАНИЕ: в .clasp.json пустой scriptId — clasp не поймёт, куда "
            "заливать.\n"
            "  Возьмите его в редакторе скриптов: Настройки проекта → "
            "Идентификатор скрипта.",
            file=sys.stderr,
        )


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for name, fn in TARGETS.items():
        path = OUT / name
        content = fn()
        if check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != content:
                stale.append(name)
        else:
            path.write_text(content, encoding="utf-8")
            print(f"собран {name} ({len(content)} байт)")

    if not check:
        warn_if_clasp_unconfigured()

    if check:
        if stale:
            print("устарели: " + ", ".join(stale), file=sys.stderr)
            print("запустите: python3 apps-script/build_gs.py", file=sys.stderr)
            return 1
        print("сгенерированные файлы актуальны")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
