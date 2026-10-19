"""Цифры еженедельного отчёта.

Здесь только чистые функции над прочитанными листами: ни одна из них не
ходит ни в таблицу, ни в сеть. Так считается всё, что потом попадёт в
Телеграм, — и так это проверяется тестами на выдуманных неделях, а не
сверкой с боевой книгой раз в семь дней.

Разделение, на котором держится отчёт: цифры считает код, формулирует их
модель. Модель не получает ни одной сырой строки таблицы — только готовый
блок чисел, который собирает numbers_text. Поэтому ошибиться в числе она
не может: его нет в её ответе, оно в её входе.

Источники откликов считаются раздельно и не смешиваются:

    каналы          метка p### из листа «Размещения»
    личные касания  метка lk_ДД_ММ из листа «Личные касания»
    без метки       прямой переход по ссылке или потерянный параметр
    чужая метка     метки, которой не соответствует ни одно размещение

У каждого из первых двух своя конверсия, и считать их вместе нельзя:
двадцать постов и двадцать сообщений в личку — это разная работа с разной
отдачей, и смысл недели в том, какая из них сработала.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from HEADERS import HEADERS
from app import checks, ru, source
# Константа плана читается через модуль, а не берётся в импорт по имени:
# её вписывают в app/config.py позже, и строка «план / факт / отклонение»
# должна появиться от одной этой правки.
from app import config
from app.config import plan_outreach, plan_placements, working_days

log = logging.getLogger(__name__)

# Эпоха Google Sheets: день 0 — 30 декабря 1899 года. Даты, прочитанные
# как UNFORMATTED_VALUE, приходят числом от этой точки.
SHEETS_EPOCH = date(1899, 12, 30)

# Форматы даты, которые встречаются в ячейке, заполненной руками. Дата,
# введённая как дата, приходит числом и до разбора строк не доходит.
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%Y/%m/%d")

WEEKDAYS_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

NO_RESPONSES_LINE = "Откликов нет"

# Размещение, у которого канал не выбран: строка заполнена датой, а канал
# забыли. Такое размещение всё равно считается — работа была сделана.
NO_CHANNEL = "— без канала"


# ------------------------------- период -----------------------------------

@dataclass(frozen=True)
class Period:
    """Неделя с понедельника по воскресенье, границы включительно."""

    start: date
    end: date

    def __contains__(self, day: date | None) -> bool:
        return day is not None and self.start <= day <= self.end

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]

    @property
    def working_days(self) -> int:
        return working_days(self.start, self.end)

    def shifted(self, weeks: int) -> "Period":
        return Period(
            self.start + timedelta(weeks=weeks), self.end + timedelta(weeks=weeks)
        )

    def title(self) -> str:
        return f"{fmt_day(self.start)}–{fmt_day(self.end)}"


def last_week(today: date) -> Period:
    """Прошедшая полная неделя. Отчёт собирается в понедельник утром.

    Считается от понедельника той недели, в которой запустили: неделя
    отчёта — предыдущая, целиком закрытая. Запуск вручную во вторник или в
    пятницу даст тот же период, а не огрызок текущей недели.
    """
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(days=7)
    return Period(start, start + timedelta(days=6))


def fmt_day(day: date) -> str:
    return day.strftime("%d.%m")


def fmt_weekday(day: date) -> str:
    return f"{WEEKDAYS_RU[day.weekday()]} {fmt_day(day)}"


def parse_date(value) -> date | None:
    """Дата из ячейки: число — серийная дата Sheets, строка — текст.

    Ничего не разобралось — None. Отчёт не вправе падать из-за того, что в
    колонку даты вписали «вторник»: такая строка просто не попадёт в неделю,
    а в отчёте это видно по числу размещений.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return SHEETS_EPOCH + timedelta(days=int(value))
    text = str(value).strip()
    if not text:
        return None
    # В created_at лежит «2026-10-16 14:03:22» — дата с временем.
    head = text.split(" ")[0].split("T")[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    # Число, приехавшее строкой: такое бывает после ручной правки формата.
    try:
        return SHEETS_EPOCH + timedelta(days=int(float(text)))
    except ValueError:
        log.warning("не разобрал дату «%s» — строка в неделю не попадёт", text[:40])
        return None


def parse_int(value) -> int:
    """Целое из ячейки. Мусор и пустота — ноль."""
    if value is None or value == "":
        return 0
    try:
        return int(float(str(value).replace(",", ".").replace(" ", "")))
    except ValueError:
        return 0


def parse_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


# ------------------------------ строки листов -----------------------------

@dataclass
class Response:
    """Одна анкета: когда пришла, откуда и чем кончилась."""

    day: date | None
    label: str
    verdict: str
    total: float | None
    stop_reasons: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        """Отсеялся на стоп-вопросах — до баллов такая анкета не доходит."""
        return bool(self.stop_reasons)

    @property
    def scored(self) -> bool:
        return not self.rejected and bool(self.verdict)


@dataclass
class Placement:
    day: date | None
    channel: str
    label: str


@dataclass
class Outreach:
    day: date | None
    sent: int
    label: str


def read_responses(values: list[list]) -> list[Response]:
    """Отклики из листа «Данные». Шапка и пустые строки отбрасываются."""
    index = {name: i for i, name in enumerate(HEADERS)}

    def cell(row: list, name: str):
        i = index[name]
        return row[i] if i < len(row) else ""

    out = []
    for row in values[1:]:
        if not str(cell(row, "id")).strip():
            continue
        stops = {f: str(cell(row, f)) for f in checks.STOP_PASS}
        out.append(
            Response(
                day=parse_date(cell(row, "created_at")),
                label=source.clean(cell(row, "source")),
                verdict=str(cell(row, "verdict")).strip(),
                total=parse_float(cell(row, "total_score")),
                stop_reasons=[
                    ru.CONDITION_NAMES.get(f, f) for f in checks.stop_failures(stops)
                ],
            )
        )
    return out


def read_placements(values: list[list], date_col: int, channel_col: int) -> list[Placement]:
    """Размещения. Метка — по номеру строки, ровно как в ссылке листа."""
    out = []
    for number, row in enumerate(values[1:], start=2):
        day = parse_date(row[date_col - 1] if len(row) >= date_col else "")
        channel = str(row[channel_col - 1] if len(row) >= channel_col else "").strip()
        if day is None and not channel:
            continue
        out.append(Placement(day=day, channel=channel, label=source.label(number)))
    return out


def read_outreach(values: list[list], date_col: int, sent_col: int) -> list[Outreach]:
    """Личные касания. Метка считается из даты, а не читается из формулы:
    так отчёт не зависит от того, успела ли таблица пересчитаться."""
    out = []
    for row in values[1:]:
        day = parse_date(row[date_col - 1] if len(row) >= date_col else "")
        sent = parse_int(row[sent_col - 1] if len(row) >= sent_col else "")
        if day is None and not sent:
            continue
        out.append(
            Outreach(
                day=day,
                sent=sent,
                label=source.outreach_label(day) if day else "",
            )
        )
    return out


# -------------------------------- подсчёт ---------------------------------

@dataclass
class DayRow:
    """День недели: сколько работы сделано и что она принесла."""

    day: date
    placements: int = 0
    outreach_sent: int = 0
    responses: int = 0

    @property
    def idle(self) -> bool:
        """Ни одного размещения и ни одного сообщения в личку."""
        return self.placements == 0 and self.outreach_sent == 0


@dataclass
class ChannelRow:
    """Канал за неделю плюс его история: выдохся он или ещё работает."""

    channel: str
    placements: int          # размещений за неделю
    responses: int           # откликов за неделю
    total_placements: int = 0   # размещений всего, за всё время
    first_yield: int | None = None   # откликов у самого первого размещения
    last_yield: int | None = None    # и у самого последнего

    @property
    def fading(self) -> bool:
        """Отдача упала от первого размещения к последнему.

        Смотрится только там, где размещались хотя бы дважды: у канала с
        одним постом первое и последнее — это он же.
        """
        if self.total_placements < 2:
            return False
        if self.first_yield is None or self.last_yield is None:
            return False
        return self.last_yield < self.first_yield


@dataclass
class WeekStats:
    """Всё, что отчёт знает о неделе. Ни одного числа мимо этого класса."""

    period: Period

    placements: int = 0
    channel_responses: int = 0
    outreach_sent: int = 0
    outreach_responses: int = 0

    responses: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    average: float | None = None
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    unlabelled: int = 0
    unknown_labels: dict[str, int] = field(default_factory=dict)

    channels: list[ChannelRow] = field(default_factory=list)
    days: list[DayRow] = field(default_factory=list)

    @property
    def silent_days(self) -> list[date]:
        """Дни без единого отклика."""
        return [d.day for d in self.days if d.responses == 0]

    @property
    def idle_days(self) -> list[date]:
        """Дни, в которые не размещались и не писали в личку."""
        return [d.day for d in self.days if d.idle]

    @property
    def per_placement(self) -> float | None:
        """Откликов на размещение. Ноль размещений — числа нет."""
        return self.channel_responses / self.placements if self.placements else None

    @property
    def outreach_rate(self) -> float | None:
        """Конверсия личных касаний, доля от отправленных."""
        return self.outreach_responses / self.outreach_sent if self.outreach_sent else None

    @property
    def empty_channels(self) -> list[str]:
        return [c.channel for c in self.channels if c.responses == 0]

    @property
    def plan_placements(self) -> int:
        return plan_placements(self.period.start, self.period.end)

    @property
    def plan_outreach(self) -> int:
        return plan_outreach(self.period.start, self.period.end)


def channel_row(
    name: str,
    placed: int,
    answered: int,
    placements: list[Placement],
    all_time: dict[str, int],
) -> ChannelRow:
    """Строка канала: неделя плюс первое и последнее размещение за всё время."""
    dated = sorted(
        (p for p in placements if (p.channel or NO_CHANNEL) == name and p.day),
        key=lambda p: p.day,
    )
    row = ChannelRow(
        channel=name,
        placements=placed,
        responses=answered,
        total_placements=len(dated),
    )
    if dated:
        row.first_yield = all_time.get(dated[0].label, 0)
        row.last_yield = all_time.get(dated[-1].label, 0)
    return row


def stats(
    period: Period,
    responses: list[Response],
    placements: list[Placement],
    outreach: list[Outreach],
) -> WeekStats:
    """Цифры одной недели. Отклики считаются по своей дате, а не по дате
    размещения: пост живёт дольше дня, и отклик с прошлой недели — отклик
    этой."""
    out = WeekStats(period=period)

    # Метка -> канал. Берутся все размещения, а не только недельные: отклик
    # на пост прошлой недели всё равно пришёл из этого канала.
    channel_of = {p.label: p.channel for p in placements}

    week_placements = [p for p in placements if p.day in period]
    out.placements = len(week_placements)

    week_outreach = [o for o in outreach if o.day in period]
    out.outreach_sent = sum(o.sent for o in week_outreach)

    week = [r for r in responses if r.day in period]
    out.responses = len(week)

    scores = []
    by_channel: dict[str, int] = {}
    for r in week:
        if r.verdict:
            out.verdicts[r.verdict] = out.verdicts.get(r.verdict, 0) + 1
        if r.rejected:
            out.rejected += 1
            for reason in r.stop_reasons:
                out.reasons[reason] = out.reasons.get(reason, 0) + 1
        elif r.scored and r.total is not None:
            scores.append(r.total)

        if not r.label:
            out.unlabelled += 1
        elif source.is_outreach(r.label):
            out.outreach_responses += 1
        elif r.label in channel_of:
            out.channel_responses += 1
            channel = channel_of[r.label]
            by_channel[channel] = by_channel.get(channel, 0) + 1
        else:
            out.unknown_labels[r.label] = out.unknown_labels.get(r.label, 0) + 1

    if scores:
        out.average = sum(scores) / len(scores)

    # Каналы недели: те, в которых размещались. Канал без единого отклика
    # должен быть в списке — именно он и есть вывод недели.
    posted: dict[str, int] = {}
    for p in week_placements:
        name = p.channel or NO_CHANNEL
        posted[name] = posted.get(name, 0) + 1

    # Отклики по метке за всё время, а не за неделю: отдача конкретного
    # поста меряется всем, что он принёс, иначе первое размещение канала
    # всегда выглядело бы пустым.
    all_time: dict[str, int] = {}
    for r in responses:
        if r.label:
            all_time[r.label] = all_time.get(r.label, 0) + 1

    out.channels = sorted(
        (
            channel_row(name, count, by_channel.get(name, 0), placements, all_time)
            for name, count in posted.items()
        ),
        key=lambda c: (-c.responses, -c.placements, c.channel),
    )

    # По дням: отдельно работа (размещения и касания) и её отдача. Провал
    # недели почти всегда виден именно здесь — в дне, когда ничего не делали.
    rows = {day: DayRow(day=day) for day in period.days}
    for p in week_placements:
        rows[p.day].placements += 1
    for o in week_outreach:
        rows[o.day].outreach_sent += o.sent
    for r in week:
        rows[r.day].responses += 1
    out.days = [rows[day] for day in period.days]

    return out


# ------------------------------ текст с цифрами ---------------------------

MINUS = "−"   # типографский минус: в отчёт идёт текст, а не код


def num(value: float | int | None, digits: int = 0) -> str:
    """Число по-русски: запятая вместо точки, пустота вместо None."""
    if value is None:
        return "—"
    if isinstance(value, int) or digits == 0:
        return str(int(round(value)))
    return f"{value:.{digits}f}".replace(".", ",")


def signed(value: float | int, digits: int = 0) -> str:
    if value == 0:
        return "без изменений"
    sign = "+" if value > 0 else MINUS
    return sign + num(abs(value), digits)


def delta(current, previous, digits: int = 0) -> str:
    """«(было 42, −5)». Нечего сравнивать — так и сказано."""
    if current is None and previous is None:
        return ""
    if previous is None:
        return " (на прошлой неделе числа нет)"
    if current is None:
        return f" (было {num(previous, digits)})"
    return f" (было {num(previous, digits)}, {signed(current - previous, digits)})"


def line(label: str, current, previous, digits: int = 0) -> str:
    return f"{label}: {num(current, digits)}{delta(current, previous, digits)}"


def plan_line(label: str, plan: int | None, fact: int) -> str:
    """План / факт / отклонение. Плана нет — строки нет."""
    if plan is None:
        return ""
    return (
        f"{label}: план {num(plan)}, факт {num(fact)}, "
        f"отклонение {signed(fact - plan)}"
    )


def percent(value: float | None) -> str:
    return "—" if value is None else num(value * 100, 1) + "%"


def numbers_text(cur: WeekStats, prev: WeekStats) -> str:
    """Блок цифр: он уходит модели и он же уходит в Телеграм при её сбое.

    Поэтому читаться должен сам по себе, без пояснений модели.
    """
    out: list[str] = []

    def add(text: str) -> None:
        """Пустую строку добавляет только тот, кто просил её явно: план,
        которого нет, не должен оставлять за собой дырку в тексте."""
        if text:
            out.append(text)

    def blank() -> None:
        out.append("")

    add(f"Неделя {cur.period.title()} (предыдущая: {prev.period.title()})")
    add(f"Рабочих дней в неделе: {cur.period.working_days}")

    blank()
    add("РАЗМЕЩЕНИЯ В КАНАЛАХ")
    add(line("Размещений", cur.placements, prev.placements))
    add(plan_line("План размещений", cur.plan_placements, cur.placements))
    add(line("Откликов из каналов", cur.channel_responses, prev.channel_responses))
    add(line("Откликов на размещение", cur.per_placement, prev.per_placement, digits=2))

    blank()
    add("ЛИЧНЫЕ КАСАНИЯ")
    add(line("Отправлено", cur.outreach_sent, prev.outreach_sent))
    add(plan_line("План касаний", cur.plan_outreach, cur.outreach_sent))
    add(line("Откликов из личек", cur.outreach_responses, prev.outreach_responses))
    add(
        f"Конверсия личных касаний: {percent(cur.outreach_rate)}"
        f" (было {percent(prev.outreach_rate)})"
    )

    blank()
    add("ОТКЛИКИ ВСЕГО")
    if cur.responses == 0:
        # Явная строка обязательна: пустая неделя — это тоже результат, и
        # отчёт о ней уходит так же, как о любой другой.
        add(f"{NO_RESPONSES_LINE}: ни одной анкеты за неделю{delta(0, prev.responses)}")
    else:
        add(line("Откликов", cur.responses, prev.responses))
    for level in ("A", "B", "C"):
        add(
            line(
                f"Уровень {level}",
                cur.verdicts.get(level, 0),
                prev.verdicts.get(level, 0),
            )
        )
    add(line("Средний балл оценённых", cur.average, prev.average, digits=1))
    add(line("Отсеялись на стоп-вопросах", cur.rejected, prev.rejected))
    for reason, count in sorted(cur.reasons.items(), key=lambda kv: (-kv[1], kv[0])):
        add(f"  причина — {reason}: {count}")
    add(line("Отклики без метки", cur.unlabelled, prev.unlabelled))
    add(
        line(
            "Отклики с чужой меткой",
            sum(cur.unknown_labels.values()),
            sum(prev.unknown_labels.values()),
        )
    )
    if cur.unknown_labels:
        add(
            "  метки, которых нет в «Размещениях»: "
            + ", ".join(f"{k} — {v}" for k, v in sorted(cur.unknown_labels.items()))
        )
    add(plan_line("План по откликам", config.PLAN_RESPONSES_WEEK, cur.responses))

    blank()
    add("ПО КАНАЛАМ ЗА НЕДЕЛЮ")
    if cur.channels:
        was_by_channel = {c.channel: c.responses for c in prev.channels}
        for c in cur.channels:
            was = was_by_channel.get(c.channel)
            text = f"{c.channel}: размещений {c.placements}, откликов {c.responses}"
            if was is not None:
                text += f" (на прошлой неделе {was})"
            if c.total_placements >= 2:
                text += (
                    f"; всего размещений {c.total_placements}, "
                    f"первое дало {num(c.first_yield)}, "
                    f"последнее {num(c.last_yield)}"
                )
            add(text)
        empty = cur.empty_channels
        add("Каналы без единого отклика: " + (", ".join(empty) if empty else "нет"))
        fading = [c.channel for c in cur.channels if c.fading]
        add("Каналы с падающей отдачей: " + (", ".join(fading) if fading else "нет"))
    else:
        add("За неделю не размещались ни в одном канале")

    blank()
    add("ПО ДНЯМ")
    for row in cur.days:
        add(
            f"{fmt_weekday(row.day)}: размещений {row.placements}, "
            f"касаний {row.outreach_sent}, откликов {row.responses}"
        )
    if cur.silent_days:
        add(
            "Дни без единого отклика: "
            + ", ".join(fmt_weekday(d) for d in cur.silent_days)
        )
    else:
        add("Дней без откликов нет")
    if cur.idle_days:
        add(
            "Дни без работы — ни размещений, ни касаний: "
            + ", ".join(fmt_weekday(d) for d in cur.idle_days)
        )

    return "\n".join(out)
