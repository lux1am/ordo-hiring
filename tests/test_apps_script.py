"""Паритет Apps Script с Python.

Форма живёт в двух вариантах, и разойдясь они начнут писать одну и ту же
анкету по-разному: другие флаги, другой порядок колонок, другая валидация.
Эти тесты ловят расхождение до деплоя.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GS = ROOT / "apps-script"

CODE_GS = (GS / "Code.gs").read_text(encoding="utf-8")
INDEX_HTML = (GS / "Index.html").read_text(encoding="utf-8")
FORM_HTML = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")


def block(tag: str, html: str) -> str:
    return re.search(rf"<{tag}>(.*?)</{tag}>", html, re.S).group(1).strip("\n")


def gs_const(name: str) -> str:
    m = re.search(rf"^var {name} = (.+?);$", CODE_GS, re.M)
    assert m, f"в Code.gs нет константы {name}"
    return m.group(1).strip()


def test_generated_files_are_current():
    """Сгенерированное соответствует источникам."""
    r = subprocess.run(
        [sys.executable, str(GS / "build_gs.py"), "--check"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode == 0, (
        f"{r.stdout}{r.stderr}\n"
        "Источники изменились — пересоберите: python3 apps-script/build_gs.py"
    )


def test_telemetry_js_is_identical():
    """JS телеметрии переносится дословно — иначе метрики разойдутся."""
    assert block("script", INDEX_HTML) == block("script", FORM_HTML)


def test_css_is_identical():
    assert block("style", INDEX_HTML) == block("style", FORM_HTML)


@pytest.mark.parametrize(
    "gs_name,py_name",
    [
        ("PASTE_PCT_THRESHOLD", "PASTE_PCT_THRESHOLD"),
        ("SPEED_CPM_THRESHOLD_DESKTOP", "SPEED_CPM_THRESHOLD_DESKTOP"),
        ("SPEED_CPM_THRESHOLD_MOBILE", "SPEED_CPM_THRESHOLD_MOBILE"),
        ("SUSPICIOUS_MIN_LENGTH", "SUSPICIOUS_MIN_LENGTH"),
        ("TAB_AWAY_SECONDS_THRESHOLD", "TAB_AWAY_SECONDS_THRESHOLD"),
        ("TZ_OFFSET_HOURS", "TZ_OFFSET_HOURS"),
    ],
)
def test_thresholds_match_python(gs_name, py_name):
    """Пороги продублированы в Code.gs и должны совпадать с app/config.py."""
    from app import config

    assert float(gs_const(gs_name)) == float(getattr(config, py_name)), (
        f"{gs_name} в Code.gs разошёлся с config.{py_name}"
    )


def test_statuses_and_honeypot_match():
    from app import config

    assert gs_const("STATUS_NEW").strip("'\"") == config.STATUS_NEW
    assert gs_const("STATUS_DUPLICATE").strip("'\"") == config.STATUS_DUPLICATE
    assert gs_const("HONEYPOT_FIELD").strip("'\"") == config.HONEYPOT_FIELD
    assert gs_const("SHEET_NAME").strip("'\"") == config.WORKSHEET_NAME


def test_headers_gs_matches_python():
    from HEADERS import HEADERS, COL_TOTAL_SCORE, FIRST_DATA_ROW, index_of

    headers_gs = (GS / "Headers.gs").read_text(encoding="utf-8")
    names = json.loads(re.search(r"const HEADERS = (\[.*?\]);", headers_gs, re.S).group(1))
    assert names == HEADERS

    def const(name):
        return int(re.search(rf"const {name} = (\d+);", headers_gs).group(1))

    assert const("COL_FORM_END") == 34
    assert const("COL_FLAGS") == index_of("flags")
    assert const("COL_STATUS") == index_of("status")
    assert const("COL_TOTAL_SCORE") == COL_TOTAL_SCORE
    assert const("FIRST_DATA_ROW") == FIRST_DATA_ROW


def test_questions_gs_matches_json():
    questions_gs = (GS / "Questions.gs").read_text(encoding="utf-8")
    data = json.loads(re.search(r"const QUESTIONS = (\{.*\});", questions_gs, re.S).group(1))
    assert data == json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))


def test_total_score_column_is_never_written():
    """AN пишется только как формула — setValue туда попадать не должен."""
    assert "setFormula" in CODE_GS
    # Зона записи значений ограничена COL_FORM_END, то есть 34 колонками.
    assert "getRange(row, 1, 1, COL_FORM_END)" in CODE_GS
    assert not re.search(r"getRange\(row,\s*COL_TOTAL_SCORE\)\.setValue", CODE_GS)


def test_last_row_is_not_used_for_allocation():
    """getLastRow() дал бы 301 из-за формул в AN — аллокация идёт по колонке A."""
    allocate = re.search(r"function allocateRow\(sheet\)\s*\{(.*?)\n\}", CODE_GS, re.S).group(1)
    assert "getLastRow" not in allocate
    assert "getRange(1, 1, maxRows, 1)" in allocate


def test_lock_wraps_allocation_and_write():
    """Аллокация и запись должны идти под одной блокировкой."""
    write = re.search(r"function writeSubmission\(.*?\n\}", CODE_GS, re.S).group(0)
    assert "LockService.getScriptLock()" in write
    assert write.index("waitLock") < write.index("allocateRow")
    assert "releaseLock" in write


def test_web_app_is_public():
    manifest = json.loads((GS / "appsscript.json").read_text(encoding="utf-8"))
    assert manifest["webapp"]["access"] == "ANYONE_ANONYMOUS"
    assert manifest["webapp"]["executeAs"] == "USER_DEPLOYING"
