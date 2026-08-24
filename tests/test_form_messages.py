"""Тексты счётчика и подсказок исполняются, а не сверяются по строкам.

Куски общего JS вытаскиваются из templates/form.html и запускаются в node:
так тест ловит и кривое склонение, и потерянное состояние validity, а не
только пропажу подстроки. Тот же приём, что в test_radio_validation.py.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORM_HTML = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="нужен node для исполнения клиентского кода"
)


def shared_js() -> str:
    return re.search(r"<script data-shared>(.*?)</script>", FORM_HTML, re.S).group(1)


def slice_js(start: str, end: str) -> str:
    js = shared_js()
    return js[js.index(start):js.index(end)]


HELPERS = "  // Русские окончания"
CONSTS_END = "  var PAUSE_MS"


def run(js: str) -> list:
    r = subprocess.run(
        ["node", "-e", slice_js(HELPERS, CONSTS_END) + js],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ------------------------------ счётчик -----------------------------------

def state(length, min_length=80, max_length=0) -> dict:
    js = f"console.log(JSON.stringify([counterState({length}, {min_length}, {max_length})]))"
    return run(js)[0]


@pytest.mark.parametrize(
    "length,expected",
    [
        (0, "минимум 80 знаков"),
        (1, "ещё 79 знаков до минимума"),
        (78, "ещё 2 знака до минимума"),
        (79, "ещё 1 знак до минимума"),
        (80, "достаточно"),
        (200, "достаточно"),
    ],
)
def test_counter_text(length, expected):
    """Ниже минимума счётчик считает недобор, выше — не считает вовсе."""
    assert state(length)["text"] == expected


@pytest.mark.parametrize("length", [80, 200, 599])
def test_counter_hides_numbers_once_minimum_is_reached(length):
    """Цифра после минимума читалась бы как остаток лимита."""
    s = state(length, max_length=700)
    assert s["text"] == "достаточно"
    assert s["cls"] == "counter ok"


@pytest.mark.parametrize(
    "length,expected",
    [
        (601, "осталось 99 знаков до предела"),
        (698, "осталось 2 знака до предела"),
        (699, "остался 1 знак до предела"),
        (700, "осталось 0 знаков до предела"),
    ],
)
def test_counter_warns_near_limit(length, expected):
    """У предела число возвращается, но уже с обратным смыслом."""
    s = state(length, max_length=700)
    assert s["text"] == expected
    assert s["cls"] == "counter limit"


def test_limit_warning_starts_exactly_at_100():
    assert state(600, max_length=700)["text"] == "достаточно"
    assert state(601, max_length=700)["cls"] == "counter limit"


def test_no_limit_no_warning():
    """У вопросов без верхнего предела оранжевого состояния не бывает."""
    assert state(100000)["cls"] == "counter ok"


@pytest.mark.parametrize(
    "length,expected",
    [
        (0, "counter"),
        (79, "counter"),
        (80, "counter ok"),
    ],
)
def test_counter_colors(length, expected):
    assert state(length)["cls"] == expected


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "1 знак"), (2, "2 знака"), (4, "4 знака"), (5, "5 знаков"),
        (11, "11 знаков"), (12, "12 знаков"), (14, "14 знаков"),
        (21, "21 знак"), (22, "22 знака"), (25, "25 знаков"), (111, "111 знаков"),
    ],
)
def test_plural_forms(n, expected):
    """«осталось 21 знаков» в анкете смотрелось бы как опечатка."""
    assert run(f"console.log(JSON.stringify([chars({n})]))") == [expected]


# ----------------------- подсказки браузера -------------------------------

VALIDATION = "  function validationText("
VALIDATION_END = "  // Сообщение пересобирается"


def message(validity: dict, *, type_="text", title="", min_length=0, max_length=0) -> str:
    """Гоняет validationText на поле с заданным состоянием validity."""
    states = {"valid": False, "valueMissing": False, "tooShort": False, "tooLong": False}
    states.update(validity)
    el = {
        "validity": states,
        "type": type_,
        "minLength": min_length,
        "maxLength": max_length,
    }
    js = (
        slice_js(HELPERS, CONSTS_END)
        + slice_js(VALIDATION, VALIDATION_END)
        + f"""
var el = {json.dumps(el, ensure_ascii=False)};
el.getAttribute = function () {{ return {json.dumps(title, ensure_ascii=False)}; }};
console.log(JSON.stringify([validationText(el)]));
"""
    )
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)[0]


def test_valid_field_has_no_message():
    assert message({"valid": True}) == ""


def test_empty_field_message_is_russian():
    assert message({"valueMissing": True}) == "Заполните это поле"


def test_consent_has_its_own_message():
    """У чекбокса «заполните поле» звучало бы бессмысленно."""
    text = message({"valueMissing": True}, type_="checkbox")
    assert text == "Отметьте согласие — без него анкету не отправить"


def test_format_message_comes_from_title():
    """title описывает нужный формат точнее любой общей фразы."""
    assert message({}, title="Например: @username или t.me/username") == (
        "Например: @username или t.me/username"
    )


def test_format_message_falls_back_when_title_missing():
    assert message({}) == "Проверьте формат"


def test_length_messages_decline_correctly():
    assert message({"tooShort": True}, min_length=5) == "Слишком коротко: нужно хотя бы 5 знаков"
    assert message({"tooLong": True}, max_length=2000) == "Слишком длинно: не больше 2000 знаков"


def test_no_state_falls_through_to_english():
    """Ни одно состояние не должно оставить сообщение пустым.

    Пустая строка снимает custom validity, и браузер показал бы своё —
    на своём языке.
    """
    for state in ("valueMissing", "tooShort", "tooLong"):
        assert message({state: True}, min_length=5, max_length=9) != ""
    assert message({}) != "", "прочие состояния закрывает текст про формат"
