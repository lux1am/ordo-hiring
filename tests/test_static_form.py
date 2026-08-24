"""Статическая версия формы для своего домена.

dist/index.html собирается из templates/form.html подстановкой вопросов —
своим разбором шаблона, без Jinja: сборку запускает системный python3, и
зависимостей у неё быть не должно. Отсюда главный тест этого файла: тот же
шаблон рендерится настоящим Jinja и сверяется с собранной страницей. Пока
он зелёный, «свой разбор» не является отдельной реализацией со своими
правилами.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "index.html"

DIST_HTML = DIST.read_text(encoding="utf-8")
FORM_HTML = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")
CODE_GS = (ROOT / "apps-script" / "Code.gs").read_text(encoding="utf-8")
QUESTIONS = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))


def block(open_tag: str, html: str, close_tag: str = "</script>") -> str:
    m = re.search(rf"{re.escape(open_tag)}(.*?){re.escape(close_tag)}", html, re.S)
    assert m, f"не найден блок {open_tag}"
    return m.group(1).strip("\n")


def web_app_url() -> str:
    return re.search(r"^var WEB_APP_URL = '([^']*)';$", CODE_GS, re.M).group(1)


# ---------------------- разметка не разошлась с источником ----------------

def test_markup_matches_jinja_render():
    """Своя подстановка обязана давать ровно то же, что настоящий Jinja.

    Сборка обходится без Jinja намеренно (её запускает системный python3),
    но расходиться с ним ей нельзя: form.html остаётся шаблоном FastAPI и
    правится по его правилам.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from app.config import HONEYPOT_FIELD

    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    expected = env.get_template("form.html").render(
        q=QUESTIONS, data={}, honeypot=HONEYPOT_FIELD, error=""
    )

    # Отличаться собранная страница вправе ровно двумя вещами: своим
    # адаптером отправки и адресом, куда уходит анкета.
    expected = expected.replace('action="/submit"', f'action="{web_app_url()}"')
    adapter_start = expected.index("<script data-adapter>") + len("<script data-adapter>")
    adapter_end = expected.index("</script>", adapter_start)
    actual_adapter = block("<script data-adapter>", DIST_HTML)
    expected = (
        expected[:adapter_start] + "\n" + actual_adapter + "\n" + expected[adapter_end:]
    )

    assert DIST_HTML == expected + "\n"


def test_css_and_shared_js_are_identical():
    """CSS и телеметрия переносятся дословно — иначе варианты разойдутся."""
    assert block("<style>", DIST_HTML, "</style>") == block("<style>", FORM_HTML, "</style>")
    assert block("<script data-shared>", DIST_HTML) == block("<script data-shared>", FORM_HTML)


def test_page_is_self_contained():
    """Одна страница без единого внешнего запроса: домен может быть любым."""
    for tag in ("<link", "<img", "<iframe", "@import"):
        assert tag not in DIST_HTML, f"внешний ресурс: {tag}"
    assert not re.search(r"<script[^>]*\ssrc=", DIST_HTML), "подключён внешний скрипт"


def test_no_template_markers_left():
    """Ни Jinja, ни скриптлетов Apps Script в готовой странице быть не может."""
    for marker in ("{{", "{%", "<?", "?>"):
        assert marker not in DIST_HTML, f"осталась вставка {marker}"


# ------------------------- вопросы подставлены ----------------------------

def test_questions_are_baked_in():
    assert f"<title>{QUESTIONS['title']}</title>" in DIST_HTML
    assert QUESTIONS["intro"] in DIST_HTML
    assert QUESTIONS["consent_text"] in DIST_HTML

    for sq in QUESTIONS["stop_questions"]:
        assert sq["text"] in DIST_HTML, sq["field"]
        assert f'name="{sq["field"]}"' in DIST_HTML

    for oq in QUESTIONS["open_questions"]:
        assert oq["text"] in DIST_HTML, oq["field"]
        assert f'data-telemetry="{oq["field"]}"' in DIST_HTML
        assert f'data-min="{oq["min_length"]}"' in DIST_HTML


def test_honeypot_name_matches_server():
    """Приманка должна называться так же, как её ждёт Code.gs."""
    from app.config import HONEYPOT_FIELD

    assert f'name="{HONEYPOT_FIELD}"' in DIST_HTML
    assert re.search(rf"^var HONEYPOT_FIELD = '{HONEYPOT_FIELD}';$", CODE_GS, re.M)


# --------------------------- отправка -------------------------------------

def test_no_apps_script_bridge():
    """google.script.run вне Apps Script не существует.

    Проверяется вызов, а не слово: в комментарии оно упоминается законно —
    там объяснено, почему моста здесь нет.
    """
    adapter = block("<script data-adapter>", DIST_HTML)
    assert "withSuccessHandler" not in DIST_HTML
    assert "withFailureHandler" not in DIST_HTML
    assert ".submitForm(" not in adapter
    assert not re.search(r"google\.script\.run\s*\n?\s*\.", DIST_HTML)


def test_adapter_avoids_preflight():
    """Любой Content-Type, кроме простого, вызовет OPTIONS — и всё встанет."""
    adapter = block("<script data-adapter>", DIST_HTML)
    assert '"Content-Type": "text/plain;charset=utf-8"' in adapter
    # Тип упоминается в комментарии — там сказано, почему он не годится.
    assert '"Content-Type": "application/json"' not in adapter
    assert '"Content-Type": "application/x-www-form-urlencoded"' not in adapter
    assert adapter.count('"Content-Type"') == 1, "заголовок должен быть один"
    assert "JSON.stringify(data)" in adapter, "тело всё равно остаётся JSON"


def test_adapter_posts_to_web_app():
    adapter = block("<script data-adapter>", DIST_HTML)
    url = web_app_url()
    assert url, "в Code.gs пустой WEB_APP_URL — странице некуда отправлять"
    assert f'var SUBMIT_URL = "{url}";' in adapter
    assert "fetch(SUBMIT_URL" in adapter
    assert 'method: "POST"' in adapter
    assert 'fetch("/submit"' not in DIST_HTML, "адаптер FastAPI просочился в статику"


def test_adapter_returns_contract_on_failure():
    """Общий код ждёт { ok, errors } — и когда ответ не JSON тоже."""
    adapter = block("<script data-adapter>", DIST_HTML)
    assert "r.json().catch(" in adapter
    assert adapter.count("ok: false") == 2, "нет адреса и битый ответ — оба случая"
    assert "_form" in adapter, "ошибка без поля показывается полосой"


def test_no_js_fallback_posts_to_web_app():
    """Без JavaScript обычная отправка должна уходить туда же."""
    tag = re.search(r"<form\b[^>]*>", DIST_HTML).group(0)
    assert 'method="post"' in tag
    assert f'action="{web_app_url()}"' in tag


# --------------------- сборка не расходится с источниками ------------------

def test_build_check_covers_dist():
    """dist/index.html пересобирается вместе с остальным, а не руками."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "apps-script" / "build_gs.py"), "--check"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"

    source = (ROOT / "apps-script" / "build_gs.py").read_text(encoding="utf-8")
    assert '"dist/index.html": build_static' in source


def build_module():
    """Сам build_gs.py: имя папки с дефисом обычному импорту не поддаётся."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_gs", ROOT / "apps-script" / "build_gs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_refuses_unknown_template_syntax():
    """Молча собрать битую страницу нельзя — сборка обязана падать."""
    build = build_module()

    with pytest.raises(SystemExit) as e:
        build.render("{% macro x() %}{% endmacro %}")
    assert "не умеет конструкцию" in str(e.value)

    with pytest.raises(SystemExit) as e:
        build.render("{% for a in [1] %}не закрыли цикл")
    assert "не закрыт" in str(e.value)

    with pytest.raises(SystemExit) as e:
        build.render("{{ q.title }}", q=None)
    assert "не вычисляется выражение" in str(e.value)


def test_values_are_escaped_like_jinja():
    """Кавычка в тексте вопроса не должна разваливать атрибут."""
    build = build_module()

    from markupsafe import escape as jinja_escape

    for raw in ('кавычка "внутри"', "апостроф 'тут'", "знак < и &", "как есть"):
        assert build.escape(raw) == str(jinja_escape(raw)), raw


# ------------------- отправка исполняется, а не читается -------------------

node_only = pytest.mark.skipif(
    shutil.which("node") is None, reason="нужен node для исполнения адаптера"
)


def run_adapter(adapter_js: str, response: str) -> dict:
    """Гоняет реальный адаптер на подменённом fetch и возвращает, что вышло.

    response — тело функции json() ответа: успешный разбор или отказ.
    """
    script = f"""
var calls = [];
global.window = {{}};
global.fetch = function (url, opts) {{
  calls.push({{ url: url, opts: opts }});
  return Promise.resolve({{ json: function () {{ {response} }} }});
}};

{adapter_js}

window.submitPayload({{ fio: "Иванов Иван", telemetry: "{{}}" }}).then(function (res) {{
  console.log(JSON.stringify({{ calls: calls, res: res }}));
}});
"""
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@node_only
def test_adapter_sends_simple_request():
    """Запрос должен уходить ровно таким, каким его пропустят без preflight."""
    out = run_adapter(
        block("<script data-adapter>", DIST_HTML),
        "return Promise.resolve({ ok: true });",
    )

    assert len(out["calls"]) == 1
    call = out["calls"][0]
    assert call["url"] == web_app_url()
    assert call["opts"]["method"] == "POST"
    assert call["opts"]["headers"] == {"Content-Type": "text/plain;charset=utf-8"}
    assert json.loads(call["opts"]["body"]) == {"fio": "Иванов Иван", "telemetry": "{}"}
    assert out["res"] == {"ok": True}


@node_only
def test_adapter_passes_field_errors_through():
    """Ошибки по полям доходят до формы как есть — она разложит их под полями."""
    errors = {"telegram": "Telegram укажите в виде @username"}
    out = run_adapter(
        block("<script data-adapter>", DIST_HTML),
        f"return Promise.resolve({json.dumps({'ok': False, 'errors': errors})});",
    )
    assert out["res"] == {"ok": False, "errors": errors}


@node_only
def test_adapter_survives_non_json_response():
    """Страница входа Google вместо JSON не должна ронять отправку молча."""
    out = run_adapter(
        block("<script data-adapter>", DIST_HTML),
        "return Promise.reject(new Error('не JSON'));",
    )
    assert out["res"]["ok"] is False
    assert "_form" in out["res"]["errors"]


@node_only
def test_adapter_without_url_says_so():
    """Собрали без адреса — кандидат должен узнать об этом, а не гадать."""
    template = (ROOT / "templates" / "static-adapter.js").read_text(encoding="utf-8")
    out = run_adapter(
        template.replace('"__SUBMIT_URL__"', '""'),
        "return Promise.resolve({ ok: true });",
    )
    assert out["calls"] == [], "без адреса запроса быть не должно"
    assert out["res"]["ok"] is False
    assert "адрес" in out["res"]["errors"]["_form"].lower()

