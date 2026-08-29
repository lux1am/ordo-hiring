#!/usr/bin/env python3
"""Генерация Apps Script и статической страницы из источников репозитория.

Порядок колонок и код телеметрии не должны существовать в трёх вариантах —
разойдясь, они начнут писать баллы не в те ячейки и молча ломать проверки.
Поэтому всё собирается автоматически:

    HEADERS.py           -> apps-script/Headers.gs
    questions.json       -> apps-script/Questions.gs
    templates/form.html  -> apps-script/Index.html   (CSS и JS дословно)
      + templates/apps-script-index.html — заготовка разметки
    templates/form.html  -> dist/index.html          (страница целиком)
      + templates/static-adapter.js — отправка на своём домене
    CUSTOM_DOMAIN        -> dist/CNAME                (свой домен для Pages)

Заготовка лежит в templates/, а не рядом с результатом: всё, что попадает
в apps-script/, clasp считает частью проекта, и файл с плейсхолдерами
уехал бы в Apps Script лишним.

Скрипт запускается системным python3 (`npm run build`), поэтому сторонних
зависимостей у него нет: подстановку вопросов в form.html делает свой
разбор шаблона, а не Jinja. Совпадение с настоящим Jinja закреплено
тестом tests/test_static_form.py — он рендерит тот же файл обоими
способами и сверяет побайтно.

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
from app.config import HONEYPOT_FIELD  # noqa: E402

BANNER = "// СГЕНЕРИРОВАНО apps-script/build_gs.py — не редактировать вручную.\n"


def extract(open_tag: str, close_tag: str, html: str) -> str:
    # Открывающий тег ищется по числу вхождений: упомяни его кто-нибудь в
    # комментарии — и «.*?» до ближайшего закрывающего вырезал бы полстраницы
    # молча. Такое лучше ловить при сборке.
    if html.count(open_tag) != 1:
        raise SystemExit(
            f"в templates/form.html тег {open_tag} встречается "
            f"{html.count(open_tag)} раз(а), а должен ровно один.\n"
            "  Если это упоминание в комментарии — перепишите его без тега."
        )
    m = re.search(rf"{re.escape(open_tag)}(.*?){re.escape(close_tag)}", html, re.S)
    if not m:
        raise SystemExit(f"в templates/form.html не найден блок {open_tag}")
    return m.group(1).strip("\n")


def build_headers() -> str:
    names = ",\n  ".join(json.dumps(n, ensure_ascii=False) for n in H.HEADERS)
    return (
        f"{BANNER}// Источник: HEADERS.py\n\n"
        f"const HEADERS = [\n  {names}\n];\n\n"
        f"const COL_FORM_END = {H.COL_FORM_END};        // AH — последняя колонка анкеты\n"
        f"const COL_FLAGS = {H.index_of('flags')};             // AS\n"
        f"const COL_AI_MARKERS = {H.index_of('ai_markers')};        // AP\n"
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
    out = template.replace("/*__CSS__*/", extract("<style>", "</style>", form))
    # Переносится только блок <script data-shared> — телеметрия, счётчики и
    # отправка, одинаковые в обоих вариантах формы. Блок <script data-adapter>
    # остаётся у каждого свой: в Apps Script это google.script.run, в FastAPI
    # обычный fetch, и подменять один другим нельзя.
    out = out.replace("//__JS__", extract('<script data-shared>', "</script>", form))
    return out


# ---------------------- статическая страница ------------------------------
#
# Разметку статической версии нельзя писать отдельно: разойдясь с form.html,
# она начнёт спрашивать не то и не так. Поэтому form.html подставляется
# целиком — вместе с CSS и JS телеметрии, — а меняются ровно две вещи:
# адаптер отправки и адрес, куда уходит анкета.


class Context(dict):
    """Данные шаблона: доступ и по точке, и по ключу.

    Отсутствующее имя даёт пустую строку — статическая страница собирается
    пустой заготовкой, ранее введённых значений в ней не бывает.
    """

    def __getattr__(self, name):
        return self[name]

    def __missing__(self, key):
        return ""


def wrap(value):
    """Разворачивает разобранный JSON так, чтобы работало `q.title`."""
    if isinstance(value, dict):
        return Context({k: wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [wrap(v) for v in value]
    return value


def escape(value) -> str:
    """Экранирование как у markupsafe — те же символы, те же ссылки.

    Совпадение важно буквально: тест сверяет собранную страницу с тем, что
    даёт настоящий Jinja, и `&#34;` против `&quot;` его бы завалило.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&#34;")
        .replace("'", "&#39;")
    )


TOKEN_RE = re.compile(r"\{%\s*(.*?)\s*%\}|\{\{\s*(.*?)\s*\}\}", re.S)


def unsupported(tag: str) -> None:
    raise SystemExit(
        f"build_gs.py не умеет конструкцию {{% {tag} %}} из templates/form.html.\n"
        "  Поддерживаются только {% for %} и {% if %}. Либо обойдитесь ими, "
        "либо научите разбор — молча собирать битую страницу нельзя."
    )


def tokenize(template: str) -> list:
    tokens, pos = [], 0
    for m in TOKEN_RE.finditer(template):
        if m.start() > pos:
            tokens.append(("text", template[pos:m.start()]))
        if m.group(1) is not None:
            tokens.append(("tag", m.group(1)))
        else:
            tokens.append(("var", m.group(2)))
        pos = m.end()
    tokens.append(("text", template[pos:]))
    return tokens


def parse(tokens: list, i: int = 0, until: tuple = ()) -> tuple:
    """Дерево узлов. until — теги, на которых разбор вложенного блока встаёт."""
    nodes = []
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "tag":
            head = value.split(None, 1)[0]
            if head in until:
                return nodes, i
            if head == "for":
                m = re.fullmatch(r"for\s+(\w+)\s+in\s+(.+)", value, re.S)
                if not m:
                    unsupported(value)
                body, i = parse(tokens, i + 1, ("endfor",))
                nodes.append(("for", m.group(1), m.group(2), body))
            elif head == "if":
                body, i = parse(tokens, i + 1, ("endif",))
                nodes.append(("if", value[2:].strip(), body))
            else:
                unsupported(value)
        else:
            nodes.append((kind, value))
        i += 1

    if until:
        raise SystemExit(
            f"в templates/form.html не закрыт блок: ожидался {{% {until[0]} %}}"
        )
    return nodes, i


def evaluate(expr: str, ctx: dict):
    """Выражения Jinja из form.html — обычные выражения Python.

    Шаблон свой, а не пользовательский, поэтому eval здесь безопасен;
    встроенные функции всё равно убраны.
    """
    try:
        return eval(expr, {"__builtins__": {}}, ctx)   # noqa: S307
    except Exception as err:
        raise SystemExit(
            f"в templates/form.html не вычисляется выражение «{expr}»: {err}"
        )


def render_nodes(nodes: list, ctx: dict, out: list) -> None:
    for node in nodes:
        if node[0] == "text":
            out.append(node[1])
        elif node[0] == "var":
            out.append(escape(evaluate(node[1], ctx)))
        elif node[0] == "if":
            if evaluate(node[1], ctx):
                render_nodes(node[2], ctx, out)
        else:
            _, name, expr, body = node
            for item in evaluate(expr, ctx):
                render_nodes(body, {**ctx, name: item}, out)


def render(template: str, **ctx) -> str:
    nodes, _ = parse(tokenize(template))
    out: list = []
    render_nodes(nodes, ctx, out)
    html = "".join(out)

    # Уцелевшая вставка означала бы, что разбор чего-то не понял. Лучше
    # упасть при сборке, чем показать кандидату «{{ q.title }}».
    left = re.search(r"\{\{|\{%", html)
    if left:
        raise SystemExit(
            "в собранной странице остались шаблонные вставки: "
            + html[left.start():left.start() + 60]
        )

    # Jinja по умолчанию снимает один перевод строки в конце шаблона.
    return html[:-1] if html.endswith("\n") else html


def web_app_url() -> str:
    """Адрес приёма анкеты берётся из Code.gs — второго места для него нет."""
    code = (OUT / "Code.gs").read_text(encoding="utf-8")
    m = re.search(r"^var WEB_APP_URL = \'([^\']*)\';$", code, re.M)
    if m is None:
        raise SystemExit("в apps-script/Code.gs не найдена константа WEB_APP_URL")
    return m.group(1)


def swap_adapter(html: str, adapter_js: str) -> str:
    """Меняет адаптер отправки: у статической версии он свой."""
    open_tag = "<script data-adapter>"
    # Та же ловушка, что в extract(): второе вхождение тега — и вырезано
    # будет не то. Проверка стоит здесь же, у самой замены.
    if html.count(open_tag) != 1:
        raise SystemExit(
            f"тег {open_tag} встречается {html.count(open_tag)} раз(а), "
            "а должен ровно один"
        )
    m = re.search(rf"({re.escape(open_tag)})(.*?)(</script>)", html, re.S)
    if m is None:
        raise SystemExit(f"в templates/form.html не найден блок {open_tag}")
    return html[:m.start(2)] + "\n" + adapter_js.strip("\n") + "\n" + html[m.end(2):]


def build_static() -> str:
    form = (ROOT / "templates" / "form.html").read_text(encoding="utf-8")
    questions = wrap(json.loads((ROOT / "questions.json").read_text(encoding="utf-8")))

    # error пустой, data пустая: страница статическая, отрисовать чужой ввод
    # или серверную ошибку ей неоткуда — это делает JS уже в браузере.
    html = render(
        form,
        q=questions,
        data=Context(),
        honeypot=HONEYPOT_FIELD,
        error="",
    )

    url = web_app_url()
    adapter = (ROOT / "templates" / "static-adapter.js").read_text(encoding="utf-8")
    html = swap_adapter(html, adapter.replace('"__SUBMIT_URL__"', json.dumps(url)))

    # Действие формы — запасной путь без JavaScript: обычный POST уходит в
    # то же веб-приложение и получает в ответ страницу «спасибо».
    old_action = 'action="/submit"'
    if html.count(old_action) != 1:
        raise SystemExit("в templates/form.html не найден action=\"/submit\"")
    html = html.replace(old_action, 'action="' + escape(url) + '"')

    return html + "\n"


# Свой домен для GitHub Pages. Файл обязан лежать в самой публикуемой папке
# и собираться вместе с ней: Pages разворачивает артефакт целиком, и всё,
# чего в артефакте нет, при деплое пропадает. CNAME, положенный руками или
# заведённый только в настройках репозитория, стёрся бы первым же прогоном,
# а вместе с ним отвалился бы и домен.
CUSTOM_DOMAIN = "hr.aiordo.ru"


def build_cname() -> str:
    return CUSTOM_DOMAIN + "\n"


TARGETS = {
    "apps-script/Headers.gs": build_headers,
    "apps-script/Questions.gs": build_questions,
    "apps-script/Index.html": build_index,
    "dist/index.html": build_static,
    "dist/CNAME": build_cname,
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


def warn_if_web_app_url_unset() -> None:
    """Без адреса статическая страница соберётся, но отправлять ей некуда."""
    if web_app_url():
        return
    print(
        "ВНИМАНИЕ: в apps-script/Code.gs пустой WEB_APP_URL — статическая "
        "страница собрана без адреса приёма.\n"
        "  Впишите URL развёртывания (…/exec) и пересоберите, иначе форма "
        "покажет кандидату ошибку вместо отправки.",
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stale = []
    for name, fn in TARGETS.items():
        path = ROOT / name
        content = fn()
        if check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != content:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"собран {name} ({len(content)} байт)")

    if not check:
        warn_if_clasp_unconfigured()
        warn_if_web_app_url_unset()

    if check:
        if stale:
            print("устарели: " + ", ".join(stale), file=sys.stderr)
            print("запустите: python3 apps-script/build_gs.py", file=sys.stderr)
            return 1
        print("сгенерированные файлы актуальны")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
