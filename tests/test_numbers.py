"""Числа Q1 и Q4: формат ячейки, разбор обратно и паритет трёх реализаций.

Одна и та же анкета может прийти через FastAPI, через Apps Script или со
статической страницы. Строку в колонке собирают разные реализации —
app/numbers.py и Code.gs, — а разбирает её потом одна. Разойдясь на пробел,
они молча потеряют числа: обработчик увидит текст без цифр и поставит ноль.
Здесь это расхождение ловится до деплоя.
"""
import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from app.numbers import (
    MonthNumbers,
    Q1Numbers,
    Q4Numbers,
    format_q1,
    format_q4,
    month_label,
    normalize_month,
    parse_int,
    parse_q1,
    parse_q4,
    recent_months,
    validate_q1,
    validate_q4,
)

ROOT = Path(__file__).resolve().parent.parent
CODE_GS = (ROOT / "apps-script" / "Code.gs").read_text(encoding="utf-8")
FORM_HTML = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="нужен node для исполнения клиентского кода"
)

MONTHS = [
    ("2026-03", 4, 480000, 120000),
    ("2026-04", 6, 720000, 120000),
    ("2026-05", 3, 330000, 110000),
]
DEFINITION = "Закрытой считал сделку по дате акта — договор не в счёт."
METHOD = "Считал за квартал по выгрузке из CRM."


def q1() -> Q1Numbers:
    return Q1Numbers(
        months=[MonthNumbers(month=m, deals=d, revenue=r, check=c) for m, d, r, c in MONTHS]
    )


def q4() -> Q4Numbers:
    return Q4Numbers(conv_touch=11, conv_deal=30, cycle_days=40)


# ------------------------------ формат ------------------------------------

def test_q1_line_is_readable():
    line = format_q1(q1(), DEFINITION)
    assert line.startswith("Март 2026: 4 / 480000 / 120000; ")
    assert "Март 2026: 4 / 480000 / 120000" in line
    assert "Апрель 2026: 6 / 720000 / 120000" in line
    assert "Май 2026: 3 / 330000 / 110000" in line
    assert line.endswith(DEFINITION)


def test_q4_line_is_readable():
    assert format_q4(q4(), METHOD) == (
        "Касание→встреча: 11%; Встреча→оплата: 30%; Цикл сделки: 40 дн — " + METHOD
    )


def test_year_is_always_written():
    """Без года январская анкета с месяцами прошлого года читается как свежая."""
    assert month_label("2026-05") == "Май 2026"
    assert month_label("2025-12") == "Декабрь 2025"
    assert month_label("мусор") == ""


@pytest.mark.parametrize(
    "text",
    [
        DEFINITION,                                   # тире внутри текста
        "Считал по оплате; акт подписывали позже",     # точка с запятой
        "Сделка = оплата 100%: аванс 50% / остаток",    # проценты и слэши
        "",                                            # текста нет вовсе
    ],
)
def test_round_trip_survives_tricky_text(text):
    """Разбор возвращает ровно то, что собрали, — что бы ни было в тексте."""
    line = format_q1(q1(), text)
    restored, tail = parse_q1(line)

    assert [(m.month, m.deals, m.revenue, m.check) for m in restored.months] == MONTHS
    assert tail == text
    assert format_q1(restored, tail) == line, "пересборка разошлась с исходником"


def test_q4_round_trip():
    line = format_q4(q4(), METHOD)
    restored, tail = parse_q4(line)
    assert (restored.conv_touch, restored.conv_deal, restored.cycle_days) == (11, 30, 40)
    assert tail == METHOD
    assert format_q4(restored, tail) == line


def test_old_answers_parse_as_text():
    """Ответы, записанные до перехода на числовые поля, не ломают разбор."""
    old = "Март: 4 сделки, выручка 480 000 ₽, средний чек 120 000 ₽."
    numbers, text = parse_q1(old)
    assert numbers.months == []
    assert text == old


# ------------------------------ месяцы ------------------------------------

@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 8, 24), ["2026-05", "2026-06", "2026-07"]),
        (date(2026, 1, 15), ["2025-10", "2025-11", "2025-12"]),
        (date(2026, 3, 1), ["2025-12", "2026-01", "2026-02"]),
    ],
)
def test_recent_months(today, expected):
    """Текущий месяц не считается: он ещё не закончился."""
    assert recent_months(today, 3) == expected


@pytest.mark.parametrize(
    "value,expected,case",
    [
        ("2026-05", "2026-05", "обычный месяц"),
        ("", "2026-07", "поле пустое"),
        ("не месяц", "2026-07", "мусор"),
        ("2026-13", "2026-07", "тринадцатого месяца нет"),
        ("2026-08", "2026-07", "текущий месяц ещё не полный"),
        ("2030-01", "2026-07", "будущее"),
        ("2000-01", "2026-07", "слишком давно"),
        ("2025-09", "2025-09", "год назад — ещё принимаем"),
    ],
)
def test_normalize_month(value, expected, case):
    """Скрытому полю не доверяем, но и анкету из-за него не отклоняем."""
    assert normalize_month(value, date(2026, 8, 24), "2026-07") == expected, case


# ---------------------------- проверка полей -------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("4", 4), ("0", 0), ("  7 ", 7),
        ("260 000", 260000),          # обычный пробел от автоформатирования
        ("260 000", 260000),     # неразрывный — его ставит редактор таблиц
        ("", None), ("-1", None), ("4.5", None), ("4,5", None), ("много", None),
    ],
)
def test_parse_int(value, expected):
    """Целое и неотрицательное; пробелы внутри суммы — не ошибка кандидата."""
    assert parse_int(value, 1000000) == expected


def test_parse_int_respects_limit():
    assert parse_int("1000", 1000) == 1000
    assert parse_int("1001", 1000) is None


def raw_form(**over) -> dict:
    data = {}
    for slot in ("m1", "m2", "m3"):
        data[f"q1_{slot}_deals"] = "4"
        data[f"q1_{slot}_revenue"] = "480000"
        data[f"q1_{slot}_check"] = "120000"
    data.update({"q4_conv_touch": "11", "q4_conv_deal": "30", "q4_cycle_days": "40"})
    data.update(over)
    return data


def test_all_nine_fields_are_required():
    _, errors = validate_q1(raw_form(q1_m2_check=""), date(2026, 8, 24))
    assert errors == {"q1_m2_check": "Укажите число"}


def test_error_names_the_field_it_belongs_to():
    """Ошибку нужно показать под полем, а не полосой над всей анкетой."""
    _, errors = validate_q1(raw_form(q1_m1_deals="пять", q1_m3_revenue="9" * 12),
                            date(2026, 8, 24))
    assert set(errors) == {"q1_m1_deals", "q1_m3_revenue"}
    assert "целое" in errors["q1_m1_deals"]
    assert "большое" in errors["q1_m3_revenue"]


def test_q4_percent_over_hundred_is_rejected():
    _, errors = validate_q4(raw_form(q4_conv_touch="120"))
    assert "q4_conv_touch" in errors


def test_zeroes_are_valid():
    """Ноль — законный ответ: месяц без сделок бывает."""
    numbers, errors = validate_q1(
        raw_form(**{f"q1_{s}_{c}": "0" for s in ("m1", "m2", "m3")
                    for c in ("deals", "revenue", "check")}),
        date(2026, 8, 24),
    )
    assert errors == {}
    assert numbers.filled() == []


def test_validate_fills_months_from_today():
    numbers, errors = validate_q1(raw_form(), date(2026, 8, 24))
    assert errors == {}
    assert [m.month for m in numbers.months] == ["2026-05", "2026-06", "2026-07"]


# --------------------------- паритет реализаций ----------------------------

def gs_numbers_block() -> str:
    """Кусок Code.gs с форматированием чисел — он исполняется в node."""
    start = CODE_GS.index("// ============================= Числа")
    end = CODE_GS.index("// =========================== Валидация")
    return CODE_GS[start:end]


def run_node(js: str):
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_apps_script_builds_the_same_line():
    """Code.gs и numbers.py обязаны собрать строку знак в знак.

    Разойдись они хоть на пробел — обработчик разберёт числа только у
    анкет, пришедших через одну из форм.
    """
    months = json.dumps(
        [{"month": m, "deals": d, "revenue": r, "check": c} for m, d, r, c in MONTHS],
        ensure_ascii=False,
    )
    js = (
        gs_numbers_block()
        + f"\nvar q1 = formatQ1({months}, {json.dumps(DEFINITION, ensure_ascii=False)});"
        + "\nvar q4 = formatQ4({conv_touch: 11, conv_deal: 30, cycle_days: 40}, "
        + f"{json.dumps(METHOD, ensure_ascii=False)});"
        + "\nconsole.log(JSON.stringify([q1, q4]));"
    )
    gs_q1, gs_q4 = run_node(js)

    assert gs_q1 == format_q1(q1(), DEFINITION)
    assert gs_q4 == format_q4(q4(), METHOD)


@needs_node
def test_apps_script_and_python_agree_on_months():
    """Запасной расчёт месяцев на обоих бэкендах одинаковый."""
    js = (
        gs_numbers_block()
        + "\nconsole.log(JSON.stringify(recentMonths(new Date(2026, 0, 15), 3)));"
    )
    assert run_node(js) == recent_months(date(2026, 1, 15), 3)


@needs_node
def test_browser_and_server_agree_on_months():
    """Браузер подписывает блоки теми же месяцами, что подставит сервер.

    Названия месяцев считает JS: статическая страница собирается один раз
    при деплое, и зашить их в неё нельзя. Сервер присланному значению не
    верит и пересчитывает сам — расчёты обязаны совпасть, иначе подпись над
    полем и месяц в таблице разъедутся.
    """
    shared = re.search(r"<script data-shared>(.*?)</script>", FORM_HTML, re.S).group(1)
    helpers = shared[shared.index("  var MONTHS_RU"):shared.index("  var REQUIRED_TEXT")]
    js = helpers + "\nconsole.log(JSON.stringify(monthSlots(new Date(2026, 0, 15), 3)));"

    slots = run_node(js)
    assert [s["value"] for s in slots] == recent_months(date(2026, 1, 15), 3)
    assert [s["label"] for s in slots] == [
        month_label(m) for m in recent_months(date(2026, 1, 15), 3)
    ]
