"""Проверка множественного выбора и ранжирования исполняется, а не сверяется.

Функции вытаскиваются из общего JS и запускаются в node на подставном DOM:
так тест ловит ошибку в логике, а не только пропажу подстроки. Тот же приём,
что в test_radio_validation.py.

Эти проверки браузер не сделает сам: чекбоксы скрыты стилями (required на
них не показал бы сообщения), а про совпадение мест в ранжировании ему
вовсе ничего не известно.
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

MOTIVES = [
    "Высокий доход без потолка",
    "Уйти на удалёнку",
    "Попробовать продажи впервые",
    "Сменить сферу",
    "Другое",
]
OTHER = "Другое"


def shared_js() -> str:
    return re.search(r"<script data-shared>(.*?)</script>", FORM_HTML, re.S).group(1)


def slice_js(start: str, end: str) -> str:
    js = shared_js()
    return js[js.index(start):js.index(end)]


def choice_code() -> str:
    """normalize + разбор групп + syncChoiceGroup, без привязки к DOM."""
    return (
        slice_js("  function normalize(text)", "  // Русские окончания")
        # Конец — привязка обработчиков: она уже требует настоящий DOM.
        # Отступ в маркере обязателен: та же строка встречается внутри
        # validateChoiceGroups, но с вложенным отступом.
        + slice_js("  var CHOICE_MIN_TEXT", '\n  nodes("[data-choice-group]").forEach(')
    )


def rank_code() -> str:
    return (
        slice_js("  function nodes(selector)", "  function boxesIn(group)")
        + slice_js("  var PLACE_UNIQUE_TEXT", '\n  nodes("[data-rank-group] select")')
    )


# Подставной DOM: ровно то, что читает проверяемый код, и ничего больше.
DOM = """
function makeBoxes(options, chosen) {
  return options.map(function (value) {
    return {
      value: value,
      checked: chosen.indexOf(value) !== -1,
      disabled: false,
      parentNode: { classList: { toggle: function (cls, on) { this.off = on; } } }
    };
  });
}

function makeChoiceForm(spec) {
  var boxes = makeBoxes(spec.options, spec.chosen);
  var attrs = {
    "data-choice-group": spec.name,
    "data-min-choices": String(spec.min),
    "data-max-choices": String(spec.max)
  };
  var group = {
    getAttribute: function (key) { return attrs[key]; },
    querySelectorAll: function () { return boxes; }
  };
  var other = spec.other === undefined ? null : {
    name: spec.name + "_other",
    value: spec.other,
    getAttribute: function (key) {
      return key === "data-other-value" ? spec.otherValue : null;
    }
  };
  var box = { hidden: null };
  return {
    boxes: boxes,
    otherInput: other,
    otherBox: box,
    form: {
      querySelectorAll: function (selector) {
        return selector === "[data-choice-group]" ? [group] : [];
      },
      querySelector: function (selector) {
        if (selector.indexOf("data-other-for") !== -1) return other;
        if (selector.indexOf("data-other-box") !== -1) return box;
        return null;
      }
    },
    group: group
  };
}

function makeRankForm(name, values) {
  var selects = values.map(function (v) { return { value: v }; });
  var group = {
    getAttribute: function () { return name; },
    querySelectorAll: function () { return selects; }
  };
  return {
    querySelectorAll: function (selector) {
      return selector === "[data-rank-group]" ? [group] : [];
    }
  };
}
"""


def run_choice(spec: dict) -> dict:
    script = f"""
{DOM}
var dom = makeChoiceForm({json.dumps(spec, ensure_ascii=False)});
var form = dom.form;
{choice_code()}
syncChoiceGroup(dom.group);
console.log(JSON.stringify({{
  errors: validateChoiceGroups(),
  disabled: dom.boxes.filter(function (b) {{ return b.disabled; }})
                     .map(function (b) {{ return b.value; }}),
  otherHidden: dom.otherBox.hidden,
  otherValue: dom.otherInput ? dom.otherInput.value : null
}}));
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def run_rank(values: list[str]) -> dict:
    script = f"""
{DOM}
var form = makeRankForm("flag_rank", {json.dumps(values)});
{rank_code()}
console.log(JSON.stringify(validateRankGroups()));
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def spec(chosen, other=None, other_value=OTHER) -> dict:
    out = {
        "name": "flag_motive", "options": MOTIVES, "chosen": chosen,
        "min": 1, "max": 2, "otherValue": other_value,
    }
    if other is not None:
        out["other"] = other
    return out


# ----------------------- множественный выбор -------------------------------

def test_one_choice_passes():
    assert run_choice(spec([MOTIVES[0]], other=""))["errors"] == {}


def test_two_choices_pass():
    assert run_choice(spec(MOTIVES[:2], other=""))["errors"] == {}


def test_nothing_chosen_is_an_error():
    out = run_choice(spec([], other=""))
    assert out["errors"] == {"flag_motive": "Выберите хотя бы один вариант"}


def test_three_choices_are_an_error():
    """Форма до трёх не доводит, но проверка обязана существовать сама по себе."""
    out = run_choice(spec(MOTIVES[:3], other=""))
    assert out["errors"] == {"flag_motive": "Выберите не больше двух вариантов"}


def test_limit_disables_the_rest():
    """Выбранный предел показывается, а не объясняется после отправки."""
    out = run_choice(spec(MOTIVES[:2], other=""))
    assert set(out["disabled"]) == set(MOTIVES[2:])

    out = run_choice(spec(MOTIVES[:1], other=""))
    assert out["disabled"] == [], "до предела гасить нечего"


def test_other_requires_text():
    out = run_choice(spec([OTHER], other=""))
    assert out["errors"] == {"flag_motive_other": "Напишите свой вариант"}


def test_other_with_text_passes():
    out = run_choice(spec([OTHER], other="Хочу работать голосом"))
    assert out["errors"] == {}


def test_other_with_only_spaces_is_empty():
    """Длина везде меряется по схлопнутым пробелам — и здесь тоже."""
    out = run_choice(spec([OTHER], other="   \n  "))
    assert out["errors"] == {"flag_motive_other": "Напишите свой вариант"}


def test_other_box_follows_the_checkbox():
    """Поле «свой вариант» видно только когда выбран сам вариант."""
    out = run_choice(spec([OTHER], other="что-то"))
    assert out["otherHidden"] is False
    assert out["otherValue"] == "что-то"

    out = run_choice(spec([MOTIVES[0]], other="забытый текст"))
    assert out["otherHidden"] is True
    assert out["otherValue"] == "", "скрытое поле не должно уехать на сервер"


# --------------------------- ранжирование ----------------------------------

def test_full_permutation_passes():
    assert run_rank(["1", "2", "3", "4"]) == {}
    assert run_rank(["4", "3", "2", "1"]) == {}


def test_duplicate_place_is_an_error():
    assert run_rank(["1", "1", "3", "4"]) == {
        "flag_rank": "Каждое место должно быть занято один раз"
    }


def test_empty_place_is_left_to_the_browser():
    """Пустое место ловит required на самом select — он видим."""
    assert run_rank(["1", "", "3", "4"]) == {}


def test_client_texts_match_server():
    """Один и тот же отказ не должен объясняться двумя разными фразами."""
    from app import profile

    js = shared_js()
    for const, expected in (
        ("CHOICE_MIN_TEXT", profile.FEW_CHOICES_RU),
        ("CHOICE_MAX_TEXT", profile.MANY_CHOICES_RU),
        ("OTHER_REQUIRED_TEXT", profile.OTHER_REQUIRED_RU),
        ("PLACE_UNIQUE_TEXT", profile.PLACE_UNIQUE_RU),
        ("PLACE_REQUIRED_TEXT", profile.PLACE_REQUIRED_RU),
    ):
        found = re.search(rf'var {const} = "([^"]+)"', js)
        assert found, f"в общем JS нет константы {const}"
        assert found.group(1) == expected, const
