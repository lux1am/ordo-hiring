"""Метка размещения: чистка значения и то, что метку вообще кладут в анкету.

Чистка живёт в трёх местах — app/source.py, общий JS формы и Code.gs, — и
разойдясь они дадут разные метки в зависимости от того, через какую форму
пришёл кандидат. Тогда в листе «Эффективность» один и тот же канал
развалился бы на две строки, а половина откликов уехала бы в «метка не
найдена». Поэтому все три реализации гоняются здесь на одних данных.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import source

ROOT = Path(__file__).resolve().parent.parent
CODE_GS = (ROOT / "apps-script" / "Code.gs").read_text(encoding="utf-8")
FORM_HTML = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "apps-script" / "Index.html").read_text(encoding="utf-8")
DIST_HTML = (ROOT / "dist" / "index.html").read_text(encoding="utf-8")

VARIANTS = [
    (FORM_HTML, "form.html"),
    (INDEX_HTML, "Index.html"),
    (DIST_HTML, "dist/index.html"),
]
VARIANT_IDS = [name for _, name in VARIANTS]

# Значения, на которых сверяются реализации. Половина — то, что действительно
# прилетит в адресе: метка из «Размещений», чужой регистр, приписанная UTM.
SAMPLES = [
    "p002",
    "P002",
    "  p002  ",
    "p002&utm_source=tg",
    "tg-channel_1",
    "канал",
    "p002канал",
    "<script>alert(1)</script>",
    "p 002",
    "p\n002",
    "",
    "-" * 3,
    "a" * 60,
    "ПP002",
]


# --------------------------- чистка в Python ------------------------------

@pytest.mark.parametrize(
    "raw,clean",
    [
        ("p002", "p002"),
        ("P002", "p002"),
        ("  p002  ", "p002"),
        ("tg-channel_1", "tg-channel_1"),
        ("p002&utm_source=tg", "p002utm_sourcetg"),
        ("канал", ""),
        ("<script>", "script"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_keeps_only_the_label_alphabet(raw, clean):
    assert source.clean(raw) == clean


def test_clean_cuts_off_at_the_limit():
    assert source.clean("a" * 100) == "a" * source.SOURCE_MAX_LEN
    assert len(source.clean("a" * 100)) == 40


def test_label_is_the_row_number_with_leading_zeros():
    assert source.label(2) == "p002"
    assert source.label(17) == "p017"
    assert source.label(300) == "p300"


def test_label_survives_its_own_cleaning():
    """Метка, которую выдаёт таблица, не должна меняться при приёме."""
    for row in (2, 7, 99, 100, 301):
        assert source.clean(source.label(row)) == source.label(row)


# ------------------- та же чистка в JS: форма и Code.gs -------------------

node_only = pytest.mark.skipif(
    shutil.which("node") is None, reason="нужен node для исполнения JS формы"
)


def clean_source_js(text: str, name: str) -> str:
    """Функция cleanSource как она написана в этом файле. Фигурных скобок в
    теле нет, поэтому вырезается до первого закрывающего на своём отступе."""
    m = re.search(r"function cleanSource\(raw\) \{.*?\n\s*\}", text, re.S)
    assert m, f"{name}: нет функции cleanSource"
    return m.group(0)


def run_clean_source(js: str, value) -> str:
    script = (
        js
        + "\nvar SOURCE_MAX_LEN = SOURCE_MAX_LEN_OVERRIDE;\n"
        + f"console.log(JSON.stringify(cleanSource({json.dumps(value)})));\n"
    )
    # Предел объявлен в своём файле рядом с функцией, а здесь подставляется
    # из Python: сверяется и он тоже.
    script = script.replace(
        "SOURCE_MAX_LEN_OVERRIDE", str(source.SOURCE_MAX_LEN)
    )
    r = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@node_only
@pytest.mark.parametrize("value", SAMPLES, ids=[repr(v) for v in SAMPLES])
def test_clean_source_matches_python(value):
    """Code.gs и общий JS формы чистят метку так же, как app/source.py."""
    expected = source.clean(value)
    assert run_clean_source(clean_source_js(CODE_GS, "Code.gs"), value) == expected
    for html, name in VARIANTS:
        js = clean_source_js(html, name)
        assert run_clean_source(js, value) == expected, name


@node_only
def test_clean_source_survives_a_missing_parameter():
    """URLSearchParams.get отдаёт null, когда параметра в адресе нет."""
    js = clean_source_js(FORM_HTML, "form.html")
    assert run_clean_source(js, None) == ""


def test_limit_is_the_same_everywhere():
    assert f"var SOURCE_MAX_LEN = {source.SOURCE_MAX_LEN};" in CODE_GS
    for html, name in VARIANTS:
        assert f"var SOURCE_MAX_LEN = {source.SOURCE_MAX_LEN};" in html, name


# ------------------------- метка доезжает до формы ------------------------

@pytest.mark.parametrize("html,name", VARIANTS, ids=VARIANT_IDS)
def test_form_has_a_hidden_field_for_the_label(html, name):
    assert re.search(
        r'<input type="hidden" name="source" id="source">', html
    ), f"{name}: нет скрытого поля метки"
    # required на скрытом поле молча застопорил бы отправку у всех.
    assert 'name="source" id="source" required' not in html


@pytest.mark.parametrize("html,name", VARIANTS, ids=VARIANT_IDS)
def test_label_is_read_from_the_url_into_the_field(html, name):
    assert 'URLSearchParams(window.location.search).get("src")' in html, name
    assert "sourceField.value = readSource();" in html, name


def test_label_goes_out_with_the_answers():
    """Поле лежит внутри формы, а не рядом: его собирает FormData."""
    form = re.search(r'<form[^>]*id="form">(.*?)</form>', DIST_HTML, re.S)
    assert form, "в собранной странице не найдена форма"
    assert 'name="source"' in form.group(1)
