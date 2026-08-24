"""Разбор тела POST в doPost исполняется, а не сверяется по строкам.

Функция parseBody вытаскивается из Code.gs и запускается в node: у неё два
вызывающих с разным форматом тела, и подменить один другим — значит молча
потерять анкету. Тот же приём, что в test_radio_validation.py.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parent.parent
CODE_GS = (ROOT / "apps-script" / "Code.gs").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="нужен node для исполнения кода Apps Script"
)


def parse_body_js() -> str:
    m = re.search(r"function parseBody\(e\) \{.*?\n\}", CODE_GS, re.S)
    assert m, "в Code.gs нет функции parseBody"
    return m.group(0)


def parse_body(event) -> dict:
    script = f"""
{parse_body_js()}
console.log(JSON.stringify(parseBody({json.dumps(event, ensure_ascii=False)})));
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


ANKETA = {"fio": "Иванов Иван", "telegram": "@ivanov", "pdn_consent": "1"}


def test_json_body_is_parsed():
    """Статическая страница шлёт JSON-строку — поля только в postData."""
    out = parse_body({"postData": {"contents": json.dumps(ANKETA)}, "parameter": {}})
    assert out["json"] is True
    assert out["params"] == ANKETA


def test_urlencoded_body_falls_back_to_parameter():
    """Обычная отправка формы: поля разобраны движком, тело — не JSON."""
    out = parse_body({
        "postData": {"contents": urlencode(ANKETA)},
        "parameter": ANKETA,
    })
    assert out["json"] is False, "ответ такому запросу — HTML, а не JSON"
    assert out["params"] == ANKETA


def test_missing_post_data_falls_back():
    assert parse_body({"parameter": ANKETA}) == {"params": ANKETA, "json": False}


def test_empty_event_does_not_crash():
    """Пустой вызов не должен ронять doPost: анкету потом отвергнет validate."""
    assert parse_body({}) == {"params": {}, "json": False}


@pytest.mark.parametrize("raw", ["", "не json", "{битый", "null"])
def test_broken_body_falls_back(raw):
    out = parse_body({"postData": {"contents": raw}, "parameter": ANKETA})
    assert out["json"] is False
    assert out["params"] == ANKETA


@pytest.mark.parametrize("raw", ["123", '"строка"', "[1, 2]", "true"])
def test_non_object_json_is_not_an_anketa(raw):
    """Анкета — всегда объект. Число или список приходят не от формы."""
    out = parse_body({"postData": {"contents": raw}, "parameter": ANKETA})
    assert out["json"] is False
    assert out["params"] == ANKETA


def test_json_answer_is_json_not_html():
    """Ответ на JSON-запрос обязан быть JSON: страницу статике показывать негде."""
    do_post = re.search(r"function doPost\(e\) \{.*?\n\}", CODE_GS, re.S).group(0)
    assert "if (body.json)" in do_post
    assert "jsonResponse(result)" in do_post
    assert do_post.index("jsonResponse") < do_post.index("renderThanks"), (
        "проверка формата должна идти раньше отрисовки страницы"
    )

    fn = re.search(r"function jsonResponse\(result\) \{.*?\n\}", CODE_GS, re.S).group(0)
    assert "ContentService" in fn
    assert "MimeType.JSON" in fn
    assert "JSON.stringify(result)" in fn


def test_both_paths_share_validation():
    """Проверки одни и те же — разный только ответ."""
    do_post = re.search(r"function doPost\(e\) \{.*?\n\}", CODE_GS, re.S).group(0)
    assert do_post.count("submitForm(") == 1, "второй вызов означал бы вторую логику"
