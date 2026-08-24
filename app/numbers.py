"""Числа Q1 и Q4: сборка компактной строки и разбор её обратно.

Колонки таблицы не меняются: девять чисел Q1 и три числа Q4 живут в тех же
ячейках q1_text и q4_text, что и раньше, — компактной строкой, а следом за
ней текстовый ответ. Строка обязана читаться человеком в таблице и
разбираться кодом на обработке, поэтому формат описан здесь один раз, а
сборка и разбор стоят рядом: разъехавшись, они молча потеряли бы числа.

Раньше числа доставал экстрактор — вызов модели по свободному тексту. Теперь
кандидат вводит их сам в отдельные поля, и весь путь стал детерминированным.
"""
import re
from dataclasses import dataclass, field
from datetime import date

# --- Месяцы ---------------------------------------------------------------

MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

YM_RE = re.compile(r"^(\d{4})-(\d{2})$")

# Насколько давние месяцы форма готова принять. Открытая с вечера вкладка,
# чужой часовой пояс и отправка «последних трёх полных» месяцев в начале
# года — всё это законно расходится с датой на сервере, поэтому проверяется
# не точное совпадение, а разумность периода.
MONTH_MAX_AGE = 24


def month_label(ym: str) -> str:
    """«2026-05» → «Май 2026». Год пишется всегда.

    Без года январская анкета с месяцами октябрь-декабрь читается как
    свежая, а колонку смотрят и через полгода после отбора.
    """
    m = YM_RE.match(ym or "")
    if not m:
        return ""
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return ""
    return f"{MONTHS_RU[month - 1]} {year}"


def month_from_label(name: str, year: str) -> str:
    """«Март», «2026» → «2026-03». Обратное к month_label.

    Разбор обязан вернуть месяц в том же виде, в каком его принимает
    сборка строки: иначе пересобранная из разобранного ячейка теряет
    названия месяцев, а вместе с ними и сами числа.
    """
    try:
        index = MONTHS_RU.index(name.capitalize()) + 1
    except ValueError:
        return ""
    return f"{year}-{index:02d}"


def recent_months(today: date, count: int = 3) -> list[str]:
    """Последние `count` полных месяцев, самый ранний первым.

    Полный — значит завершившийся: месяц самой даты не считается, о нём
    кандидату ещё нечего сказать.
    """
    year, month = today.year, today.month
    out = []
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append(f"{year}-{month:02d}")
    return list(reversed(out))


def normalize_month(value: str, today: date, fallback: str) -> str:
    """Месяц из формы или запасной, если пришёл мусор.

    Месяц приходит скрытым полем, то есть из браузера: доверять ему нельзя,
    но и отклонять анкету из-за него нечестно — кандидат этого поля не
    видел. Поэтому непригодное значение молча заменяется расчётным.
    """
    m = YM_RE.match(str(value or "").strip())
    if not m:
        return fallback
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return fallback
    age = (today.year - year) * 12 + (today.month - month)
    if age < 1 or age > MONTH_MAX_AGE:
        return fallback
    return f"{year}-{month:02d}"


# --- Числа ----------------------------------------------------------------

@dataclass
class MonthNumbers:
    """Один месяц Q1. Числа целые и неотрицательные — так их принимает форма."""
    month: str = ""
    deals: int = 0
    revenue: int = 0
    check: int = 0

    def is_filled(self) -> bool:
        """Месяц с результатом. Ноль сделок — это честный пустой месяц."""
        return self.deals > 0 and self.revenue > 0


@dataclass
class Q4Numbers:
    """Три метрики воронки. Конверсии в процентах, цикл в днях."""
    conv_touch: int = 0
    conv_deal: int = 0
    cycle_days: int = 0


@dataclass
class Q1Numbers:
    months: list[MonthNumbers] = field(default_factory=list)

    def filled(self) -> list[MonthNumbers]:
        return [m for m in self.months if m.is_filled()]


def parse_int(value, limit: int) -> int | None:
    """Целое от 0 до limit. None — значение непригодно.

    Пробелы и неразрывные пробелы внутри числа снимаются: их ставит
    автоформатирование сумм, и «260 000» — это не ошибка кандидата.
    """
    text = str(value if value is not None else "").strip()
    text = re.sub(r"[\s ]", "", text)
    if not re.fullmatch(r"\d+", text):
        return None
    number = int(text)
    if number > limit:
        return None
    return number


# --- Компактная строка ----------------------------------------------------

# Числа и текст разделяет тире с пробелами. Разбор на него не опирается —
# тире встречается и внутри ответа, — но человеку так читается легче.
TEXT_SEP = " — "

Q1_MONTH_RE = re.compile(
    r"([А-ЯЁ][а-яё]+)\s+(\d{4}):\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)"
)

Q4_TOUCH_RE = re.compile(r"Касание→встреча:\s*(\d+)\s*%")
Q4_DEAL_RE = re.compile(r"Встреча→оплата:\s*(\d+)\s*%")
Q4_CYCLE_RE = re.compile(r"Цикл сделки:\s*(\d+)\s*дн")


def format_q1(numbers: Q1Numbers, text: str) -> str:
    """«Май 2026: 4 / 260000 / 65000; … — что считал закрытой сделкой»."""
    parts = [
        f"{month_label(m.month)}: {m.deals} / {m.revenue} / {m.check}"
        for m in numbers.months
    ]
    line = "; ".join(parts)
    text = (text or "").strip()
    if not line:
        return text
    return line + TEXT_SEP + text if text else line


def format_q4(numbers: Q4Numbers, text: str) -> str:
    """«Касание→встреча: 12%; Встреча→оплата: 30%; Цикл сделки: 21 дн — …»."""
    line = (
        f"Касание→встреча: {numbers.conv_touch}%; "
        f"Встреча→оплата: {numbers.conv_deal}%; "
        f"Цикл сделки: {numbers.cycle_days} дн"
    )
    text = (text or "").strip()
    return line + TEXT_SEP + text if text else line


def _tail(line: str, end: int) -> str:
    """Текстовая часть — всё после последнего числового куска."""
    return line[end:].lstrip(" ;.—-").strip()


def parse_q1(line: str) -> tuple[Q1Numbers, str]:
    """Разбирает ячейку q1_text обратно в числа и текст.

    Числа не находятся — это не ошибка обработки: значит в таблице лежит
    ответ, записанный до перехода на числовые поля. Такой кандидат получит
    ноль за Q1 по коду, и это видно в баллах.
    """
    matches = list(Q1_MONTH_RE.finditer(line or ""))
    months = [
        MonthNumbers(
            month=month_from_label(m.group(1), m.group(2)),
            deals=int(m.group(3)),
            revenue=int(m.group(4)),
            check=int(m.group(5)),
        )
        for m in matches
    ]
    text = _tail(line or "", matches[-1].end()) if matches else (line or "").strip()
    return Q1Numbers(months=months), text


def parse_q4(line: str) -> tuple[Q4Numbers, str]:
    line = line or ""
    touch = Q4_TOUCH_RE.search(line)
    deal = Q4_DEAL_RE.search(line)
    cycle = Q4_CYCLE_RE.search(line)

    numbers = Q4Numbers(
        conv_touch=int(touch.group(1)) if touch else 0,
        conv_deal=int(deal.group(1)) if deal else 0,
        cycle_days=int(cycle.group(1)) if cycle else 0,
    )
    ends = [m.end() for m in (touch, deal, cycle) if m]
    text = _tail(line, max(ends)) if ends else line.strip()
    return numbers, text


# --- Проверка полей формы -------------------------------------------------
#
# Ошибка должна встать под своим полем, поэтому проверка возвращает словарь
# «имя поля → текст», как это делает validate() в Code.gs, а не исключение
# на всю анкету.

EMPTY_RU = "Укажите число"
NOT_INT_RU = "Только целое число, без знаков и дробной части"
TOO_BIG_RU = "Число слишком большое"


def _question(field: str) -> dict:
    from app.config import load_questions
    for q in load_questions()["open_questions"]:
        if q["field"] == field:
            return q
    return {}


def _cell(raw: dict, name: str, limit: int, errors: dict[str, str]) -> int:
    value = str(raw.get(name, "") or "").strip()
    if not value:
        errors[name] = EMPTY_RU
        return 0
    number = parse_int(value, limit)
    if number is None:
        errors[name] = TOO_BIG_RU if re.fullmatch(r"[\d\s ]+", value) else NOT_INT_RU
        return 0
    return number


def validate_q1(raw: dict, today: date) -> tuple[Q1Numbers, dict[str, str]]:
    """Девять чисел Q1 плюс месяцы, к которым они относятся."""
    schema = _question("q1_text").get("months", {})
    slots = schema.get("slots", [])
    columns = schema.get("columns", [])
    expected = recent_months(today, len(slots))

    errors: dict[str, str] = {}
    months: list[MonthNumbers] = []

    for i, slot in enumerate(slots):
        prefix = f"q1_{slot}_"
        month = MonthNumbers(
            month=normalize_month(raw.get(prefix + "month"), today, expected[i])
        )
        for column in columns:
            value = _cell(raw, prefix + column["key"], column["max"], errors)
            setattr(month, column["key"], value)
        months.append(month)

    return Q1Numbers(months=months), errors


def validate_q4(raw: dict) -> tuple[Q4Numbers, dict[str, str]]:
    """Три метрики воронки."""
    metrics = _question("q4_text").get("metrics", [])
    errors: dict[str, str] = {}
    values = {}
    # Ключ значения — имя поля без префикса вопроса: q4_conv_touch → conv_touch.
    for metric in metrics:
        name = metric["field"]
        values[name[len("q4_"):]] = _cell(raw, name, metric["max"], errors)
    return Q4Numbers(**values), errors
