"""FastAPI-приложение: форма, приём анкеты, запуск обработки.

НЕ ИСПОЛЬЗУЕТСЯ при деплое на Google Apps Script — там форму отдаёт
apps-script/Code.gs, а строку пишет doPost напрямую через SpreadsheetApp.
Модуль оставлен для варианта с веб-сервером на Render; см. README, раздел
«Два варианта деплоя». Тесты его по-прежнему покрывают, чтобы вариант
оставался рабочим.

Обработка при варианте с Apps Script запускается отдельно — process_new.py.
"""
import json
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import ratelimit, recovery, sheets, source as src
from app.config import (
    BASE_DIR,
    HONEYPOT_FIELD,
    STATUS_DUPLICATE,
    load_questions,
    missing_secrets,
    setup_logging,
)
from app.checks import profile_flags, stop_failures, telemetry_flags
from app.models import OPEN_FIELDS, Submission, Telemetry
from app.profile import pack_markers, validate_profile, validate_stops
from app.pipeline import build_form_row, build_result_row, new_id, process_row
from app import ru

setup_logging()
log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

_scanner_stop = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = missing_secrets()
    if missing:
        # Не падаем: форма должна отдаваться, чтобы Render не крутил рестарты,
        # а причина видна и в логах, и в /health.
        log.error("НЕ ЗАДАНЫ переменные окружения: %s", ", ".join(missing))
    else:
        try:
            sheets.ensure_header()
        except Exception:
            log.exception("не удалось проверить шапку листа")
        recovery.recover(startup=True)

    global _scanner_stop
    _scanner_stop = recovery.start_scanner()
    yield
    if _scanner_stop:
        _scanner_stop.set()


app = FastAPI(title="Отбор кандидатов", lifespan=lifespan)


@app.get("/health")
def health():
    missing = missing_secrets()
    return {"status": "ok" if not missing else "misconfigured", "missing_env": missing}


@app.get("/", response_class=HTMLResponse)
def form(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="form.html",
        context={"q": load_questions(), "honeypot": HONEYPOT_FIELD, "error": None, "data": {}},
    )


FORM_ERROR_KEY = "_form"


def _wants_json(request: Request) -> bool:
    """Форма отправляет анкету через fetch и ждёт JSON.

    Обычный POST — путь для случая, когда JavaScript отключён или упал:
    тогда показывать результат на месте формы некому, и ответом идёт
    страница. То же разделение, что у doPost в Apps Script.
    """
    return "application/json" in request.headers.get("accept", "")


def _errors_by_field(exc: ValidationError) -> dict[str, str]:
    """Ошибки pydantic — в словарь «имя поля → текст».

    Проверки отдельных полей дают имя в loc, проверки анкеты целиком — нет;
    последние собираются под общим ключом и показываются полосой.
    """
    errors: dict[str, str] = {}
    loose: list[str] = []
    for err in exc.errors():
        message = err["msg"].replace("Value error, ", "")
        field = str(err["loc"][0]) if err["loc"] else ""
        if field and field not in {"stops", "answers", "telemetry"}:
            errors.setdefault(field, message)
        else:
            loose.append(message)
    if loose:
        errors[FORM_ERROR_KEY] = "; ".join(loose)
    return errors


def _render_error(
    request: Request, errors: dict[str, str], data: dict, code: int = 400
):
    if _wants_json(request):
        return JSONResponse({"ok": False, "errors": errors}, status_code=code)
    return templates.TemplateResponse(
        request=request,
        name="form.html",
        context={
            "q": load_questions(),
            "honeypot": HONEYPOT_FIELD,
            "error": "; ".join(errors.values()),
            "data": data,
        },
        status_code=code,
    )


def _thanks(request: Request):
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return templates.TemplateResponse(request=request, name="thanks.html", context={})


@app.post("/submit", response_class=HTMLResponse)
async def submit(request: Request, background: BackgroundTasks):
    # Форма нужна целиком, а не словарём: у множественного выбора одно имя
    # и несколько значений, и dict() оставил бы только последнее.
    form = await request.form()
    raw = dict(form)
    ip = ratelimit.client_ip(request)

    # Приманка для ботов: поле скрыто стилями, человек его не видит.
    # Заполнено — отвечаем как обычно, но в таблицу ничего не пишем.
    if str(raw.get(HONEYPOT_FIELD, "")).strip():
        log.warning("honeypot сработал, адрес %s — анкета не записана", ip)
        return _thanks(request)

    if not ratelimit.allow(ip):
        return _render_error(
            request,
            {FORM_ERROR_KEY: "С вашего адреса отправлено слишком много анкет. "
                             "Попробуйте через час."},
            raw,
            code=429,
        )

    try:
        telemetry = Telemetry.model_validate_json(raw.get("telemetry") or "{}")
    except ValidationError:
        log.warning("телеметрия не разобралась, продолжаю с пустой")
        telemetry = Telemetry()

    # Стоп-вопросы и вопросы-флаги проверяются до анкеты: ошибку про
    # конкретный вариант нужно вернуть с именем его поля, чтобы форма
    # показала её под этим вопросом.
    stops, choice_errors = validate_stops(form)
    profile, profile_errors = validate_profile(form)
    choice_errors.update(profile_errors)
    if choice_errors:
        log.info("варианты ответов не прошли проверку: %s", choice_errors)
        return _render_error(request, choice_errors, raw)

    try:
        sub = Submission(
            fio=str(raw.get("fio", "")),
            telegram=str(raw.get("telegram", "")),
            resume_url=str(raw.get("resume_url", "")),
            pdn_consent=bool(raw.get("pdn_consent")),
            stops=stops,
            profile=profile,
            answers={f: str(raw.get(f, "")) for f in OPEN_FIELDS},
            telemetry=telemetry,
        )
    except ValidationError as e:
        errors = _errors_by_field(e)
        log.info("анкета не прошла валидацию: %s", errors)
        return _render_error(request, errors, raw)

    submission_id = new_id()
    # Метка размещения: форма подставляет её из ?src= в адресе ссылки.
    # Чистится ещё раз здесь — в поле могло приехать что угодно.
    source = src.clean(raw.get("source"))
    flags = telemetry_flags(sub) + profile_flags(sub.profile)
    # Ответы о мотивации кладём в AP сразу: отдельной колонки под них нет, а
    # обработчик их уже не увидит — в анкетной зоне A..AH их не бывает.
    markers = pack_markers(sub.profile, [])
    values = build_form_row(sub, submission_id)

    # Логируем payload до записи: диск Render не переживает рестарт, а логи
    # остаются — если Google API отвалится, анкету можно восстановить руками.
    log.info(
        "ПОЛУЧЕНА АНКЕТА %s",
        json.dumps(
            {
                "id": submission_id,
                "ip": ip,
                "flags": flags,
                "source": source,
                "values": values,
            },
            ensure_ascii=False,
        ),
    )

    try:
        duplicate = sheets.telegram_exists(sub.telegram)
        row = sheets.allocate_row()
        if duplicate:
            log.warning("строка %d: дубль по телеграму %s, модель не вызываем", row, sub.telegram)
            sheets.write_form(
                row, values, ", ".join(flags),
                status=STATUS_DUPLICATE, markers=markers, source=source,
            )
            sheets.write_result(
                row,
                scores=None,
                result=build_result_row(
                    flags=flags,
                    profile=sub.profile,
                    status=STATUS_DUPLICATE,
                    conditions=ru.conditions_ru(stop_failures(sub.stops)),
                    decision="Повторная анкета",
                    flags_text=ru.flags_ru(
                        flags,
                        [f"анкета с {sub.telegram} уже есть в таблице"],
                        sub.profile,
                    ),
                ),
            )
            return _thanks(request)

        sheets.write_form(
            row, values, ", ".join(flags), markers=markers, source=source
        )
    except Exception:
        log.exception("анкета %s: не удалось записать в таблицу", submission_id)
        return _render_error(
            request,
            {FORM_ERROR_KEY: "Не удалось сохранить анкету. "
                             "Попробуйте отправить ещё раз через минуту."},
            raw,
            code=500,
        )

    log.info(
        "анкета %s записана в строку %d, метка «%s», флаги: %s",
        submission_id, row, source or "—", flags or "нет",
    )
    background.add_task(process_row, row)
    return _thanks(request)
