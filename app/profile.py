"""Стоп-вопросы и вопросы-флаги: проверка полей формы и сборка ячеек.

Ошибка должна встать под своим полем, поэтому проверки возвращают словарь
«имя поля → текст», как это делает validate() в Code.gs, а не исключение на
всю анкету. Раньше здесь же жил разбор числовых полей Q1 и Q4 — числа из
анкеты убраны вместе с ними.

Колонок в листе по-прежнему 54, и новых под вопросы-флаги не появилось.
Ответы на них едут в колонке ai_markers: там лежит JSON с двумя ключами —
markers от модели и profile с ответами кандидата, см. pack_markers ниже.
Уточнение к стоп-вопросу — часовой пояс — приписывается к ячейке своего
вопроса через STOP_SEP.
"""
import json
import logging

from app.config import load_questions

log = logging.getLogger(__name__)

# Между вариантом ответа и уточнением к нему. Тире с пробелами: внутри самих
# вариантов есть короткое тире без пробелов («10–14»), и разбор их не путает.
STOP_SEP = " — "

EMPTY_CHOICE_RU = "Выберите один вариант"
UNKNOWN_CHOICE_RU = "Такого варианта в анкете нет"
EMPTY_TEXT_RU = "Заполните это поле"
TOO_LONG_RU = "Слишком длинный ответ"
FEW_CHOICES_RU = "Выберите хотя бы один вариант"
MANY_CHOICES_RU = "Выберите не больше двух вариантов"
OTHER_REQUIRED_RU = "Напишите свой вариант"
PLACE_REQUIRED_RU = "Расставьте все четыре места"
PLACE_UNIQUE_RU = "Каждое место должно быть занято один раз"


def stop_questions() -> list[dict]:
    return load_questions()["stop_questions"]


def flag_questions() -> list[dict]:
    return load_questions()["flag_questions"]


def question(field: str) -> dict:
    """Стоп-вопрос или вопрос-флаг по имени поля."""
    for q in stop_questions() + flag_questions():
        if q["field"] == field:
            return q
    return {}


# --- Ячейка стоп-вопроса --------------------------------------------------

def format_stop(value: str, extra_label: str, extra: str) -> str:
    """«Да» + «Ваш часовой пояс» + «МСК+2» → «Да — Ваш часовой пояс: МСК+2»."""
    value = (value or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return value
    return f"{value}{STOP_SEP}{extra_label}: {extra}"


def stop_option(cell: str) -> str:
    """Вариант ответа из ячейки, без приписанного уточнения."""
    return str(cell or "").split(STOP_SEP)[0].strip()


# --- Проверка полей формы -------------------------------------------------

def validate_stops(raw: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Ответы на стоп-вопросы вместе с уточнениями, готовые для ячеек."""
    stops: dict[str, str] = {}
    errors: dict[str, str] = {}

    for q in stop_questions():
        field = q["field"]
        value = str(raw.get(field, "") or "").strip()
        if not value:
            errors[field] = EMPTY_CHOICE_RU
        elif value not in q["options"]:
            errors[field] = UNKNOWN_CHOICE_RU

        extra = q.get("extra")
        if not extra:
            stops[field] = value
            continue

        text = " ".join(str(raw.get(extra["field"], "") or "").split())
        if not text:
            errors[extra["field"]] = EMPTY_TEXT_RU
        elif len(text) > extra["max_length"]:
            errors[extra["field"]] = TOO_LONG_RU
        stops[field] = format_stop(value, extra["label"], text)

    return stops, errors


def _multi(q: dict, raw: dict, errors: dict[str, str], extras: dict) -> list[str]:
    """Множественный выбор. Значений может прийти несколько под одним именем."""
    field = q["field"]
    getter = getattr(raw, "getlist", None)
    values = list(getter(field)) if getter else _as_list(raw.get(field))
    chosen = [str(v).strip() for v in values if str(v).strip()]

    unknown = [v for v in chosen if v not in q["options"]]
    if unknown:
        errors[field] = UNKNOWN_CHOICE_RU
        return []
    if len(chosen) < q["min_choices"]:
        errors[field] = FEW_CHOICES_RU
        return []
    if len(chosen) > q["max_choices"]:
        errors[field] = MANY_CHOICES_RU
        return []

    other = q.get("other_option")
    if other and other in chosen:
        text = " ".join(str(raw.get(field + "_other", "") or "").split())
        if not text:
            errors[field + "_other"] = OTHER_REQUIRED_RU
        elif len(text) > q["other_max_length"]:
            errors[field + "_other"] = TOO_LONG_RU
        else:
            # Ключ появляется только когда выбран сам вариант: пустая строка
            # в каждой анкете была бы шумом в и без того плотной ячейке.
            extras[field + "_other"] = text

    return chosen


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _single(q: dict, raw: dict, errors: dict[str, str], extras: dict) -> str:
    field = q["field"]
    value = str(raw.get(field, "") or "").strip()
    if not value:
        errors[field] = EMPTY_CHOICE_RU
        return ""
    if value not in q["options"]:
        errors[field] = UNKNOWN_CHOICE_RU
        return ""
    return value


def _rank(q: dict, raw: dict, errors: dict[str, str], extras: dict) -> dict[str, str]:
    """Ранжирование: каждое место занято ровно один раз."""
    places = q["places"]
    out: dict[str, str] = {}
    for row in q["rows"]:
        value = str(raw.get(row["field"], "") or "").strip()
        if value not in places:
            errors[q["field"]] = PLACE_REQUIRED_RU
            return {}
        out[row["field"]] = value

    if len(set(out.values())) != len(places):
        errors[q["field"]] = PLACE_UNIQUE_RU
        return {}
    return out


HANDLERS = {"multi": _multi, "single": _single, "rank": _rank}


def validate_profile(raw: dict) -> tuple[dict, dict[str, str]]:
    """Ответы на вопросы-флаги. Значение — список, строка или места по полям."""
    profile: dict = {}
    extras: dict = {}
    errors: dict[str, str] = {}

    for q in flag_questions():
        profile[q["field"]] = HANDLERS[q["type"]](q, raw, errors, extras)
    profile.update(extras)

    if errors:
        log.info("вопросы-флаги не прошли проверку: %s", errors)
    return profile, errors


# --- Ячейка ai_markers ----------------------------------------------------
#
# Отдельной колонки под ответы о мотивации нет, а знать их рекрутёру нужно.
# Они едут вместе с признаками машинного текста в одной ячейке: markers
# пишет модель при обработке, profile — форма при сабмите, и обработчик
# обязан вычитать ячейку перед записью, иначе ответы кандидата затрутся.
# Ровно так же устроена колонка flags, см. sheets.read_flags.


def pack_markers(profile: dict | None, markers: list[str] | None) -> str:
    """JSON для колонки ai_markers. Пусто с обеих сторон — пустая ячейка."""
    if not profile and not markers:
        return ""
    return json.dumps(
        {"markers": list(markers or []), "profile": profile or {}},
        ensure_ascii=False,
    )


def unpack_markers(cell) -> tuple[list[str], dict]:
    """Разбирает ячейку обратно в признаки и ответы.

    Строка, не разобравшаяся как объект, — это ячейка, записанная до
    перехода на JSON: там лежали признаки через запятую. Такую читаем как
    признаки с пустым профилем, а не роняем обработку.
    """
    text = str(cell or "").strip()
    if not text:
        return [], {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return [m.strip() for m in text.split(",") if m.strip()], {}

    markers = data.get("markers") or []
    profile = data.get("profile") or {}
    return (
        [str(m) for m in markers] if isinstance(markers, list) else [],
        profile if isinstance(profile, dict) else {},
    )
