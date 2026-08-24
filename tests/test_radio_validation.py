"""Проверка радиогрупп исполняется, а не сверяется по строкам.

Функции вытаскиваются из общего JS и запускаются в node с фейковым DOM:
так тест ловит ошибку в логике, а не только пропажу подстроки.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORM_HTML = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")
CODE_GS = (ROOT / "apps-script" / "Code.gs").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="нужен node для исполнения клиентского кода"
)

STOP_FIELDS = [
    "stop_1_oklad", "stop_2_komissiya", "stop_3_kasaniya",
    "stop_4_vstrechi", "stop_5_vedenie", "stop_6_crm",
]


def shared_js() -> str:
    return re.search(r"<script data-shared>(.*?)</script>", FORM_HTML, re.S).group(1)


def extract_validator() -> str:
    """Кусок общего кода от radioGroups до конца validateRadioGroups."""
    js = shared_js()
    start = js.index("var RADIO_REQUIRED_TEXT")
    end = js.index("// Ошибка под группой")
    return js[start:end]


def run_validator(checked: dict[str, bool]) -> dict:
    """Гоняет реальный код на фейковой форме и возвращает найденные ошибки."""
    inputs = []
    for field in STOP_FIELDS:
        for value in ("Да", "Нет"):
            inputs.append(
                {"name": field, "checked": checked.get(field) == value}
            )

    script = f"""
const inputs = {json.dumps(inputs, ensure_ascii=False)};
const form = {{ querySelectorAll: () => inputs }};
{extract_validator()}
console.log(JSON.stringify(validateRadioGroups()));
"""
    r = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_nothing_chosen_flags_every_group():
    errors = run_validator({})
    assert set(errors) == set(STOP_FIELDS), "должны отметиться все шесть групп"


def test_all_chosen_passes():
    assert run_validator({f: "Да" for f in STOP_FIELDS}) == {}


def test_mixed_answers_pass():
    chosen = {f: ("Да" if i % 2 else "Нет") for i, f in enumerate(STOP_FIELDS)}
    assert run_validator(chosen) == {}


@pytest.mark.parametrize("missing", STOP_FIELDS)
def test_single_missing_group_is_pinpointed(missing):
    chosen = {f: "Да" for f in STOP_FIELDS if f != missing}
    errors = run_validator(chosen)
    assert set(errors) == {missing}, f"ожидалась ровно одна ошибка на {missing}"


def test_client_text_matches_server():
    """Один и тот же вопрос не должен объясняться двумя разными фразами."""
    client = re.search(r'var RADIO_REQUIRED_TEXT = "([^"]+)"', shared_js()).group(1)
    assert client in CODE_GS, "текст ошибки разошёлся с серверным"


def test_runs_before_report_validity():
    """Стоп-вопросы проверяются первыми: браузер их не проверит сам."""
    js = shared_js()
    assert js.index("validateRadioGroups()") < js.index("form.reportValidity()")


def test_submission_stops_on_radio_errors():
    js = shared_js()
    block = js[js.index("var radioErrors"):js.index("form.reportValidity()")]
    assert "showErrors(radioErrors)" in block
    assert "return;" in block, "при незаполненных группах отправки быть не должно"


def test_error_clears_on_choice():
    js = shared_js()
    assert 'el.addEventListener("change"' in js
    assert "box.textContent = \"\"" in js


def test_radio_still_has_no_required():
    """Клиентская проверка не повод возвращать required на скрытое поле.

    Ищем атрибут на самом теге: в комментарии рядом слово упоминается
    законно — там объяснено, почему его там нет.
    """
    for row in re.findall(r'<div class="radio-row">(.*?)</div>', FORM_HTML, re.S):
        for tag in re.findall(r"<input\b[^>]*>", row):
            assert not re.search(r"\brequired\b", tag), f"required вернулся: {tag}"
