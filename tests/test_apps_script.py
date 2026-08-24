"""Паритет Apps Script с Python.

Форма живёт в трёх вариантах — FastAPI, Apps Script и статическая страница
на своём домене, — и разойдясь они начнут писать одну и ту же анкету
по-разному: другие флаги, другой порядок колонок, другая валидация. Эти
тесты ловят расхождение до деплоя.

Здесь общее для всех вариантов. Что есть только у статики — подстановка
вопросов, отправка без preflight — проверяет tests/test_static_form.py.
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
DIST_HTML = (ROOT / "dist" / "index.html").read_text(encoding="utf-8")

# Все варианты формы: то, что проверяется у одного, проверяется у каждого.
VARIANTS = [
    (FORM_HTML, "form.html"),
    (INDEX_HTML, "Index.html"),
    (DIST_HTML, "dist/index.html"),
]
VARIANT_IDS = [name for _, name in VARIANTS]


def block(open_tag: str, html: str, close_tag: str = "</script>") -> str:
    m = re.search(rf"{re.escape(open_tag)}(.*?){re.escape(close_tag)}", html, re.S)
    assert m, f"не найден блок {open_tag}"
    return m.group(1).strip("\n")


def shared_js(html: str) -> str:
    """Общий блок: телеметрия, счётчики, отправка через submitPayload."""
    return block("<script data-shared>", html) if "data-shared" in html else block(
        "<script>\n//__JS__", html
    )


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


def test_shared_js_is_identical():
    """Общий блок переносится дословно — иначе поведение форм разойдётся.

    В Index.html он лежит вторым: первым идёт адаптер отправки, свой у
    каждого варианта.
    """
    index_shared = INDEX_HTML.split("</script>")[1].split("<script>")[-1].strip("\n")
    assert index_shared == block("<script data-shared>", FORM_HTML)


def test_adapter_is_not_shared():
    """Адаптер отправки у каждого варианта свой и копироваться не должен."""
    adapter = block("<script data-adapter>", INDEX_HTML)
    assert "google.script.run" in adapter
    assert "submitForm" in adapter
    assert 'fetch("/submit"' not in INDEX_HTML, "адаптер FastAPI просочился в Apps Script"

    fastapi_adapter = block("<script data-adapter>", FORM_HTML)
    assert "fetch(" in fastapi_adapter
    # Ищем характерный вызов, а не слово: в комментарии оно упоминается законно.
    assert "withSuccessHandler" not in FORM_HTML, "адаптер Apps Script просочился в FastAPI"


def test_css_is_identical():
    assert block("<style>", INDEX_HTML, "</style>") == block(
        "<style>", FORM_HTML, "</style>"
    )


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


# ------------------------------- clasp ------------------------------------

CLASP_CONFIG = json.loads((ROOT / ".clasp.json").read_text(encoding="utf-8"))
CLASPIGNORE = (GS / ".claspignore").read_text(encoding="utf-8")
PACKAGE = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

# Ровно эти файлы должны уезжать в Apps Script.
PUSHED = {"appsscript.json", "Code.gs", "Headers.gs", "Questions.gs", "Index.html"}


def test_clasp_points_at_existing_project():
    """rootDir — папка со скриптом; scriptId заполняет владелец таблицы.

    Скрипт привязан к таблице, поэтому проект не создаётся заново: clasp
    должен заливать в уже существующий, иначе форма отвяжется от листа.
    """
    assert CLASP_CONFIG["rootDir"] == "apps-script"
    assert "scriptId" in CLASP_CONFIG, "без scriptId clasp создаст новый проект"
    assert isinstance(CLASP_CONFIG["scriptId"], str)


def test_headers_pushed_before_code():
    """Headers.gs и Questions.gs объявляют константы, которыми пользуется Code.gs."""
    order = CLASP_CONFIG["filePushOrder"]
    assert order.index("Headers.gs") < order.index("Code.gs")
    assert order.index("Questions.gs") < order.index("Code.gs")


def test_claspignore_is_a_whitelist():
    """Сначала исключается всё, потом добавляются нужные файлы поимённо."""
    lines = [
        ln.strip() for ln in CLASPIGNORE.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines[0] == "**/**", "белый список должен начинаться с исключения всего"
    allowed = {ln[1:] for ln in lines if ln.startswith("!")}
    assert allowed == PUSHED


def test_no_stray_pushable_files_in_apps_script():
    """В apps-script/ не должно появляться файлов, которые clasp заберёт сам.

    clasp по умолчанию игнорирует .py, .md и точечные файлы, но не .html и
    не .gs. Заготовка разметки поэтому лежит в templates/, а не здесь: файл
    с плейсхолдерами уехал бы в проект как настоящий.
    """
    risky = {
        p.name for p in GS.iterdir()
        if p.is_file() and p.suffix in {".gs", ".html", ".json"}
    }
    assert risky == PUSHED, (
        f"лишние файлы в apps-script/: {sorted(risky - PUSHED)}. "
        "Уберите их в другую папку или добавьте в PUSHED осознанно."
    )
    assert not (GS / "index.template.html").exists()
    assert (ROOT / "templates" / "apps-script-index.html").exists()


def test_push_rebuilds_before_uploading():
    """Иначе в проект уедет то, что отстало от источников."""
    push = PACKAGE["scripts"]["push"]
    assert "build" in push
    assert push.index("build") < push.index("clasp")
    assert "clasp" in push and "push" in push


def test_deploy_pushes_first():
    deploy = PACKAGE["scripts"]["deploy"]
    assert "push" in deploy
    assert deploy.index("push") < deploy.index("deploy")


def test_npm_scripts_pass_claspignore_explicitly():
    """clasp ищет .claspignore рядом с .clasp.json, а он лежит в корне.

    Без флага -I файл в apps-script/ не читается. Сейчас это не приводит к
    беде — умолчания clasp отбирают те же пять файлов, — но флаг делает
    белый список действующим, а не декоративным.
    """
    for name in ("push", "deploy"):
        assert "-I apps-script/.claspignore" in PACKAGE["scripts"][name], name


def test_clasp_is_pinned():
    assert PACKAGE["devDependencies"]["@google/clasp"].startswith("^3.")


def test_secrets_not_in_repo_config():
    assert (ROOT / ".gitignore").read_text(encoding="utf-8").count(".clasprc.json") == 1


# --------------------- валидация до отправки ------------------------------

def test_no_minlength_on_textareas():
    """minlength мерил бы сырую строку, а сервер — нормализованную.

    Ответ из 80 знаков с двойными пробелами прошёл бы браузер и упал бы на
    сервере. Длину проверяет setCustomValidity по нормализованной строке.
    """
    for html, _ in VARIANTS:
        # Тег ищем до закрывающего </textarea>: внутри есть "?>" от шаблона,
        # и обычное [^>]* оборвалось бы на нём.
        textareas = re.findall(r"<textarea\b(.*?)</textarea>", html, re.S)
        assert textareas, "textarea не найдены"
        for ta in textareas:
            assert "minlength" not in ta, f"minlength на textarea: {ta[:80]}"
            assert "data-min" in ta, "нет data-min — счётчику неоткуда взять порог"


def test_length_check_uses_normalize():
    shared = block("<script data-shared>", FORM_HTML)
    assert "function normalize(" in shared
    assert shared.count("function normalize(") == 1, "normalize должна быть одна"
    assert "setCustomValidity" in shared
    assert "normalize(el.value).length" in shared


def test_counter_shows_requirement_not_progress():
    """Счётчик говорит, сколько нужно, а не сколько набрано."""
    shared = block("<script data-shared>", FORM_HTML)
    assert '"достаточно"' in shared
    assert '"минимум " + chars(min)' in shared
    assert '"остался", "осталось", "осталось"' in shared, "нет склонения «знак»"
    assert 'length + "/" + min' not in shared, "счётчик «0/80» вернулся"
    style = block("<style>", FORM_HTML, "</style>")
    assert ".counter.ok" in style
    assert ".counter.limit" in style, "нет цвета для остатка до предела"


def test_all_validation_states_have_russian_text():
    """Нативные подсказки браузера на его языке — в русской анкете не годятся.

    Каждое состояние validity должно получать свой текст через
    setCustomValidity, иначе всплывёт «Please fill out this field».
    """
    shared = block("<script data-shared>", FORM_HTML)
    assert "function validationText(" in shared
    for state in ("valueMissing", "tooShort", "tooLong"):
        assert state in shared, f"состояние {state} без своего текста"
    assert 'el.getAttribute("title")' in shared, "формат поля описывает его title"
    assert "Заполните это поле" in shared
    assert "Отметьте согласие" in shared


def test_validity_refresh_does_not_recurse():
    """checkValidity() шлёт событие invalid — вызов из его обработчика зациклит.

    Поэтому состояние читается через el.validity: она событий не шлёт.
    """
    fn = re.search(r"function refreshValidity\(el\) \{.*?\n  \}", 
                   block("<script data-shared>", FORM_HTML), re.S)
    assert fn, "функция refreshValidity не найдена"
    assert "checkValidity" not in fn.group(0)
    assert 'el.setCustomValidity("")' in fn.group(0), "нативные проверки должны возвращаться"


def test_visible_fields_are_wired_to_custom_messages():
    shared = block("<script data-shared>", FORM_HTML)
    assert 'form.querySelectorAll("input, textarea, select")' in shared
    assert 'el.hasAttribute("data-telemetry")' in shared, "у textarea своё сообщение"
    assert 'el.addEventListener("change", refresh)' in shared


@pytest.mark.parametrize(
    "needle",
    [
        'minlength="5"',
        'maxlength="200"',
        r'pattern="\S+\s+\S+.*"',
        'maxlength="2000"',
        "t\\.me/",
    ],
)
def test_native_attributes_present(needle):
    """Атрибуты полей одинаковы во всех вариантах формы."""
    for html, name in VARIANTS:
        assert needle in html, f"нет в {name}: {needle}"


def test_every_field_has_error_slot():
    """Под каждым полем есть куда положить серверную ошибку."""
    # Поля с фиксированными именами.
    expected = {"fio", "telegram", "resume_url", "pdn_consent"}
    for html, name in VARIANTS:
        slots = set(re.findall(r'data-error-for="([a-z0-9_]+)"', html))
        assert expected <= slots, f"{name}: нет слотов для {sorted(expected - slots)}"

    # Стоп-вопросы и открытые вопросы подставляются циклом шаблона.
    assert 'data-error-for="<?= sq.field ?>"' in INDEX_HTML
    assert 'data-error-for="<?= oq.field ?>"' in INDEX_HTML
    assert 'data-error-for="{{ sq.field }}"' in FORM_HTML
    assert 'data-error-for="{{ oq.field }}"' in FORM_HTML


def test_submission_goes_through_submit_payload():
    shared = block("<script data-shared>", FORM_HTML)
    assert "window.submitPayload(collectPayload())" in shared
    assert "e.preventDefault()" in shared
    assert "form.reportValidity()" in shared, "нативные подсказки должны показываться"
    assert 'btn.textContent = busy ? "Отправляем…"' in shared
    assert "showSuccess" in shared


# ------------------- страница ошибки и её ссылка --------------------------

def test_no_history_back_link_anywhere():
    """history.back() в iframe Apps Script отматывает историю самого iframe.

    Проверяется именно ссылка: в комментарии причина упоминается законно.
    """
    for text in (CODE_GS, INDEX_HTML, FORM_HTML, DIST_HTML):
        assert "javascript:history.back" not in text
        assert "Вернуться и исправить" not in text


def test_render_error_link_is_usable():
    """Запасная страница ошибки ведёт на форму и открывается поверх iframe."""
    render_error = re.search(r"function renderError\(.*?\n\}", CODE_GS, re.S).group(0)
    assert "webAppUrl()" in render_error
    assert 'target="_top"' in render_error, "без target ссылка откроется внутри iframe"
    assert "Открыть анкету заново" in render_error


def test_fallback_path_is_kept():
    """doPost остаётся запасным путём на случай отключённого JavaScript."""
    assert "function doPost(" in CODE_GS
    assert "function renderThanks(" in CODE_GS
    assert "function renderError(" in CODE_GS
    assert "function submitForm(" in CODE_GS


def test_validate_returns_errors_by_field():
    validate = re.search(r"function validate\(params\).*?\n\}", CODE_GS, re.S).group(0)
    assert "var errors = {};" in validate, "ошибки должны быть объектом, а не списком"
    assert "return { ok: !hasKeys(errors), errors: errors, data: data };" in validate
    for field in ("errors.fio", "errors.telegram", "errors.resume_url", "errors.pdn_consent"):
        assert field in validate, f"нет ошибки для {field}"


def test_submit_form_returns_contract():
    submit = re.search(r"function submitForm\(payload\).*?\n\}\n", CODE_GS, re.S).group(0)
    assert "return { ok: true };" in submit
    assert "return { ok: false, errors: parsed.errors };" in submit
    assert "FORM_ERROR_KEY" in submit


def test_doget_keeps_allowall():
    doget = re.search(r"function doGet\(\).*?\n\}", CODE_GS, re.S).group(0)
    assert "XFrameOptionsMode.ALLOWALL" in doget


# ------------------ запасной путь без JavaScript --------------------------

def test_form_keeps_action_and_method():
    """Без action и method обычная отправка мертва, а с ней и весь fallback.

    Если у кандидата отключён или упал JavaScript, google.script.run не
    сработает и форма должна уйти обычным POST в doPost.
    """
    for html, name in VARIANTS:
        tag = re.search(r"<form\b[^>]*>", html).group(0)
        assert 'method="post"' in tag, f"{name}: нет method — уйдёт GET"
        assert "action=" in tag, f"{name}: нет action"

    apps_tag = re.search(r"<form\b[^>]*>", INDEX_HTML).group(0)
    assert "formUrl" in apps_tag, "action должен вести на адрес веб-приложения"
    assert 'action="/submit"' in re.search(r"<form\b[^>]*>", FORM_HTML).group(0)


def test_form_action_uses_configured_url():
    """formUrl приходит из webAppUrl(), а не напрямую из ScriptApp."""
    doget = re.search(r"function doGet\(\).*?\n\}", CODE_GS, re.S).group(0)
    assert "t.formUrl = webAppUrl();" in doget


# ---------------------- адрес веб-приложения ------------------------------

def test_web_app_url_constant():
    """Константа задана и, если заполнена, ведёт на рабочее развёртывание."""
    m = re.search(r"^var WEB_APP_URL = '([^']*)';$", CODE_GS, re.M)
    assert m, "нет константы WEB_APP_URL"
    url = m.group(1)
    if url:
        assert url.startswith("https://script.google.com/macros/s/"), url
        assert url.endswith("/exec"), (
            "нужен адрес рабочей версии (/exec), а не тестовой (/dev)"
        )


def test_web_app_url_falls_back_safely():
    """ScriptApp может бросить исключение — обращение к нему обёрнуто в try."""
    fn = re.search(r"function webAppUrl\(\).*?\n\}", CODE_GS, re.S).group(0)
    assert "if (WEB_APP_URL) return WEB_APP_URL;" in fn, "константа имеет приоритет"
    assert "try {" in fn and "catch" in fn
    assert "return '';" in fn, "при неудаче возвращается пустая строка"


def test_render_error_survives_unknown_url():
    """Страница ошибки не должна падать и не должна рисовать пустую ссылку."""
    fn = re.search(r"function renderError\(message\).*?\n\}", CODE_GS, re.S).group(0)
    assert "var url = webAppUrl();" in fn, "адрес берётся через устойчивую обёртку"
    assert "ScriptApp" not in fn, "прямое обращение к ScriptApp может бросить исключение"
    assert "Вернитесь на страницу анкеты и заполните заново" in fn
    assert "url\n    ?" in fn or "url ?" in fn, "ссылка рисуется только при известном адресе"
    assert 'target="_top"' in fn


def test_render_error_branches_are_exclusive():
    """Ровно две ветки: ссылка либо текст, третьего не дано."""
    fn = re.search(r"function renderError\(message\).*?\n\}", CODE_GS, re.S).group(0)
    assert fn.count("<a href=") == 1
    assert fn.count("Вернитесь на страницу") == 1


# ------------------------ подсказки у полей -------------------------------

EXPECTED_LABELS = {
    "fio": "Фамилия, имя, отчество (при наличии)",
    "telegram": "Telegram",
    "resume_url": "Ссылка на резюме или профиль на площадке",
}


@pytest.mark.parametrize("field,text", sorted(EXPECTED_LABELS.items()))
def test_field_labels_match(field, text):
    """Подписи полей одинаковы в обоих вариантах формы."""
    for html, name in VARIANTS:
        assert f'<label for="{field}">{text}</label>' in html, f"{name}: подпись {field}"


TELEMETRY_NOTICE = (
    "Форма фиксирует технические параметры заполнения — время и способ ввода. "
    "Эти данные не влияют на оценку и используются только для проверки "
    "подлинности ответов."
)


def test_telemetry_is_disclosed_next_to_consent():
    """Сбор телеметрии раскрыт там же, где согласие на обработку данных."""
    for html, name in VARIANTS:
        assert TELEMETRY_NOTICE in html, f"{name}: нет раскрытия телеметрии"
        consent = re.search(
            r'<div class="consent">.*?<div class="hint consent-note">(.*?)</div>',
            html, re.S,
        )
        assert consent, f"{name}: примечание оторвано от блока согласия"
        assert consent.group(1) == TELEMETRY_NOTICE


EXPECTED_TITLES = {
    "fio": "Укажите имя и фамилию через пробел",
    "telegram": "Например: @username или t.me/username",
    "resume_url": "Ссылка должна начинаться с http:// или https://",
}


@pytest.mark.parametrize("field,title", sorted(EXPECTED_TITLES.items()))
def test_pattern_fields_have_title(field, title):
    """Без title браузер скажет «Введите данные в требуемом формате»."""
    for html, name in VARIANTS:
        tag = re.search(rf'<input[^>]*id="{field}".*?>', html, re.S)
        assert tag, f"{name}: поле {field} не найдено"
        assert f'title="{title}"' in tag.group(0), f"{name}: нет title у {field}"


# ------------------------- адаптер отправки -------------------------------

def test_fetch_adapter_asks_for_json():
    """Без Accept уйдёт */*, и сервер отдаст HTML запасного пути."""
    adapter = block("<script data-adapter>", FORM_HTML)
    assert '"Accept": "application/json"' in adapter


def test_fetch_adapter_survives_non_json_response():
    """HTML вместо JSON не должен ронять отправку в консоль."""
    adapter = block("<script data-adapter>", FORM_HTML)
    assert ".catch(" in adapter, "разбор ответа должен быть обёрнут"
    assert "Ошибка отправки, попробуйте ещё раз" in adapter
    assert "ok: false" in adapter


# ------------- нативная валидация: novalidate и скрытые поля --------------

HIDING_PROPS = ("display:none", "visibility:hidden", "opacity:0")


def css_rules(html: str) -> dict:
    """Селектор → тело правила без пробелов."""
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    rules = {}
    if not style:
        return rules
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style.group(1)):
        norm = body.replace(" ", "").replace("\n", "")
        for sel in selectors.split(","):
            sel = sel.strip()
            if sel:
                rules[sel] = rules.get(sel, "") + norm
    return rules


def required_tags(html: str) -> list:
    """Теги полей с required. Учитывает «?>» внутри шаблонных вставок."""
    tags = re.findall(r"<(?:input|textarea)\b(?:[^>]|\?>)*?>", html, re.S)
    return [t for t in tags if re.search(r"\brequired\b", t)]


@pytest.mark.parametrize("html,name", VARIANTS, ids=VARIANT_IDS)
def test_form_has_no_novalidate(html, name):
    """Без novalidate браузер проверяет поля и на запасном пути тоже.

    Если JavaScript отключён, обработчик с reportValidity() не сработает, и
    единственная защита от отправки пустой анкеты — нативная валидация.
    """
    tag = re.search(r"<form\b[^>]*>", html).group(0)
    assert "novalidate" not in tag, f"{name}: novalidate вернулся"


@pytest.mark.parametrize("html,name", VARIANTS, ids=VARIANT_IDS)
def test_no_required_on_hidden_fields(html, name):
    """Скрытое поле с required блокирует отправку, не показав сообщения.

    Браузер не может показать подсказку у элемента, которого не видно, и
    форма молча перестаёт отправляться — у всех и сразу. Поэтому у скрытых
    полей required быть не должно, их проверяет сервер.
    """
    rules = css_rules(html)
    hidden_selectors = [
        sel for sel, body in rules.items()
        if any(prop in body for prop in HIDING_PROPS)
    ]
    # Классы, внутри которых поля скрыты: ".hp", ".radio-row input" -> hp, radio-row
    hidden_classes = set()
    for sel in hidden_selectors:
        m = re.match(r"\.([a-z0-9_-]+)", sel)
        if m and not sel.endswith(":empty"):
            hidden_classes.add(m.group(1))

    assert hidden_classes, "не найдено ни одного правила, прячущего элементы"

    for cls in hidden_classes:
        # Блок целиком: от тега с этим классом до конца секции.
        for block_match in re.finditer(
            rf'<div class="{cls}"[^>]*>(.*?)</div>\s*</div>|<div class="{cls}"[^>]*>(.*?)</div>',
            html, re.S,
        ):
            body = block_match.group(1) or block_match.group(2) or ""
            offenders = required_tags(body)
            assert not offenders, (
                f"{name}: required на скрытом поле внутри .{cls}: {offenders[:1]}"
            )


@pytest.mark.parametrize("html,name", VARIANTS, ids=VARIANT_IDS)
def test_honeypot_has_no_required(html, name):
    """required на приманке остановил бы отправку у всех пользователей."""
    hp = re.search(r'<div class="hp".*?</div>', html, re.S)
    assert hp, f"{name}: блок приманки не найден"
    assert not re.search(r"\brequired\b", hp.group(0)), f"{name}: required на приманке"


@pytest.mark.parametrize("html,name", VARIANTS, ids=VARIANT_IDS)
def test_radio_inputs_have_no_required(html, name):
    """Радиокнопки скрыты стилями — их обязательность проверяет сервер."""
    for row in re.findall(r'<div class="radio-row">(.*?)</div>', html, re.S):
        assert not required_tags(row), f"{name}: required на скрытой радиокнопке"


@pytest.mark.parametrize("html,name", VARIANTS, ids=VARIANT_IDS)
def test_visible_fields_keep_required(html, name):
    """Снятие required со скрытых полей не должно задеть видимые."""
    joined = " ".join(required_tags(html))
    for field in ("fio", "telegram", "resume_url", "pdn_consent"):
        assert f'id="{field}"' in joined, f"{name}: потерян required у {field}"

    # Внутри тега textarea есть шаблонная вставка со своим ">", поэтому
    # общий разбор тегов его не видит. В form.html между тегами ещё и
    # подставляется прежнее значение — отсюда допуск на содержимое.
    assert re.search(r"required>[^<]*</textarea>", html), (
        f"{name}: потерян required у textarea"
    )


def test_server_still_requires_stop_answers():
    """Сервер обязан ловить то, что перестал ловить браузер."""
    validate = re.search(r"function validate\(params\).*?\n\}", CODE_GS, re.S).group(0)
    assert "QUESTIONS.stop_questions" in validate
    assert "Выберите «Да» или «Нет»" in validate
    assert "errors[field]" in validate


def test_report_validity_still_called():
    """Вызов остаётся: он нужен при программной отправке."""
    assert "form.reportValidity()" in block("<script data-shared>", FORM_HTML)
