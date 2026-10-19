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
from app.config import (
    OUTREACH_SHEET,
    PLACEMENTS_SHEET,
    plan_outreach,
    plan_placements,
    working_days,
)

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
    # Колонка «Что было дальше» (outcome, BB): позвонили, вышел, отказался.
    # Её заполняет человек, и читается она только на «заполнена или нет» —
    # текст из неё не попадает ни в отчёт, ни в выгрузку: см. digest.py.
    outcome: str = ""

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
                outcome=str(cell(row, "outcome")).strip(),
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

    # Заведён ли учёт вообще. Пустой лист за всю историю — это «учёта ещё
    # нет», а не «работа не велась», и путать это нельзя: во втором случае
    # ноль — упрёк человеку, в первом — напоминание завести таблицу.
    placements_tracked: bool = False
    outreach_tracked: bool = False

    placements: int = 0
    channel_responses: int = 0
    outreach_sent: int = 0
    outreach_responses: int = 0

    responses: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    average: float | None = None
    scored: int = 0          # по сколькам анкетам посчитан средний балл
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    unlabelled: int = 0
    unknown_labels: dict[str, int] = field(default_factory=dict)

    channels: list[ChannelRow] = field(default_factory=list)
    days: list[DayRow] = field(default_factory=list)

    @property
    def tracking_started(self) -> bool:
        """Учёт заведён целиком. Нет — выводов о работе по цифрам не делают."""
        return self.placements_tracked and self.outreach_tracked

    @property
    def untracked(self) -> list[str]:
        """Названия листов, которые пусты за всю историю."""
        out = []
        if not self.placements_tracked:
            out.append(PLACEMENTS_SHEET)
        if not self.outreach_tracked:
            out.append(OUTREACH_SHEET)
        return out

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
    out = WeekStats(
        period=period,
        # Смотрим на листы целиком, а не на неделю: лист, в котором нет ни
        # одной строки за всю историю, означает, что учёт не начат.
        placements_tracked=bool(placements),
        outreach_tracked=bool(outreach),
    )

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

    out.scored = len(scores)
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
    """Блок цифр для модели: всё, что посчитано, и признак учёта.

    Признак стоит первым и назван по-машинному — tracking_started. По нему
    модель отличает «работа не велась» от «учёт ещё не начат»: во втором
    случае нулями ничего не доказывается, и выводов о работе человека по
    ним делать нельзя.
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
    add(f"tracking_started: {'true' if cur.tracking_started else 'false'}")
    if not cur.tracking_started:
        add(
            "Листы учёта пусты за всю историю, а не только за эту неделю: "
            + ", ".join(f"«{title}»" for title in cur.untracked)
            + ". Учёт по ним ещё не начат, и нули ниже — отсутствие учёта, "
            "а не отсутствие работы."
        )

    blank()
    add("РАЗМЕЩЕНИЯ В КАНАЛАХ")
    if cur.placements_tracked:
        add(line("Размещений", cur.placements, prev.placements))
        add(plan_line("План размещений", cur.plan_placements, cur.placements))
    else:
        add(f"Размещений: {NOT_TRACKED} (лист «{PLACEMENTS_SHEET}» пуст)")
    add(line("Откликов из каналов", cur.channel_responses, prev.channel_responses))
    add(line("Откликов на размещение", cur.per_placement, prev.per_placement, digits=2))

    blank()
    add("ЛИЧНЫЕ КАСАНИЯ")
    if cur.outreach_tracked:
        add(line("Отправлено", cur.outreach_sent, prev.outreach_sent))
        add(plan_line("План касаний", cur.plan_outreach, cur.outreach_sent))
    else:
        add(f"Отправлено: {NOT_TRACKED} (лист «{OUTREACH_SHEET}» пуст)")
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
    if cur.scored:
        # База у среднего балла своя — только оценённые анкеты. Пишется она
        # перед сравнением, иначе в строке окажется две скобки подряд.
        add(
            f"Средний балл: {num(cur.average, 1)} по {cur.scored} оценённым"
            + delta(cur.average, prev.average, digits=1)
        )
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
        # Нули по незаведённому листу в дневной таблице не показываются: они
        # означали бы, что в этот день не работали, а это неизвестно.
        parts = []
        if cur.placements_tracked:
            parts.append(f"размещений {row.placements}")
        if cur.outreach_tracked:
            parts.append(f"касаний {row.outreach_sent}")
        parts.append(f"откликов {row.responses}")
        add(f"{fmt_weekday(row.day)}: " + ", ".join(parts))
    if cur.silent_days:
        add(
            "Дни без единого отклика: "
            + ", ".join(fmt_weekday(d) for d in cur.silent_days)
        )
    else:
        add("Дней без откликов нет")
    # Про дни без работы можно говорить только там, где работу учитывают.
    if cur.tracking_started and cur.idle_days:
        add(
            "Дни без работы — ни размещений, ни касаний: "
            + ", ".join(fmt_weekday(d) for d in cur.idle_days)
        )

    return "\n".join(out)


# ============================ сообщение в Телеграм ========================
#
# То, что уходит в Телеграм, — не тот же текст, что уходит модели. Модель
# получает всё, что посчитано (numbers_text выше): по ней она и видит, какой
# канал пуст и в какой день не работали. Человек получает короткую сводку и
# два коротких раздела выводов — остальное он посмотрит в «Эффективности».
#
# Разметка HTML, и только жирные заголовки разделов. Ни <pre>, ни <code>:
# Телеграм рисует их блоком кода с кнопкой «копировать» — читать сводку в
# таком виде неудобно, а выглядит она технической выгрузкой. Поэтому цифры
# идут обычными строками, по одной мысли на строку, без колонок.
#
# Всё, что приходит извне — названия каналов, текст модели, — экранируется:
# одна угловая скобка в названии канала превратила бы сообщение в ошибку 400.

# Жёсткий потолок сообщения. Телеграм режет на 4096, но нам важна
# читаемость: отчёт читают с телефона в понедельник утром.
MESSAGE_LIMIT = 1500

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MONTHS_SHORT = (
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)

DASH = " — "      # между подписью и значением
DOT = " · "
ELLIPSIS = "…"

# Сколько названий каналов влезает в строку «Пусто». Остальные — числом:
# список на двадцать каналов съел бы весь потолок сообщения.
CHANNELS_IN_LINE = 5

# Предел длины названия канала в строке. Названия заводит человек, и
# «Телеграм-канал «Работа в продажах Москва и область»» — не редкость.
CHANNEL_NAME_LIMIT = 28

# Предел на раздел выводов. Промпт просит две коротких фразы, но
# рассчитывать на послушание модели нельзя: разошедшийся первый раздел
# иначе съел бы весь бюджет, и второго в сообщении не осталось бы вовсе.
SECTION_LIMIT = 300

NIKA_TITLE = "Нике"
VACANCY_TITLE = "По вакансии"

# Учёт по этому листу ещё не заведён. Ноль с дельтой на его месте читался бы
# упрёком человеку, который ещё не начал заполнять таблицу.
NOT_TRACKED = "учёт ещё не ведётся"


def esc(text) -> str:
    """Экранирование для parse_mode=HTML. Телеграму нужны ровно эти три."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def period_title(period: Period) -> str:
    """«13–19 октября», а через месяц — «28 сентября – 4 октября»."""
    start, end = period.start, period.end
    if start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS[end.month - 1]}"
    return (
        f"{start.day} {MONTHS[start.month - 1]} – {end.day} {MONTHS[end.month - 1]}"
    )


def short_days(days: list[date]) -> str:
    """«14, 15 окт». Месяц называется один раз, пока он один."""
    if not days:
        return ""
    if len({day.month for day in days}) == 1:
        numbers = ", ".join(str(day.day) for day in days)
        return f"{numbers} {MONTHS_SHORT[days[0].month - 1]}"
    return ", ".join(f"{day.day} {MONTHS_SHORT[day.month - 1]}" for day in days)


def change(current, previous, digits: int = 0) -> str:
    """Дельта в скобках со знаком: «(+11)». Нечего показать — пусто.

    Ноль дельтой не считается: «(+0)» в сводке — шум, а не новость.
    """
    if current is None or previous is None or current == previous:
        return ""
    sign = "+" if current > previous else MINUS
    return f" ({sign}{num(abs(current - previous), digits)})"


def short_name(text: str, limit: int = CHANNEL_NAME_LIMIT) -> str:
    """Название канала, укороченное до предела."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + ELLIPSIS


def listing(items: list[str], limit: int = CHANNELS_IN_LINE) -> str:
    """Перечисление с хвостом числом: «А, Б, В и ещё 4»."""
    names = [short_name(item) for item in items[:limit]]
    tail = f" и ещё {len(items) - limit}" if len(items) > limit else ""
    return ", ".join(names) + tail


def plan_tail(plan: int | None) -> str:
    """«из 100» рядом с фактом. Плана нет — и хвоста нет."""
    return f" из {plan}" if plan is not None else ""


def scored_note(count: int) -> str:
    """«(по 5 оценённым)». Средний балл считается не по всем откликам, и
    читать его как среднее по всем — ошибка, которая стоит решений."""
    one = count % 10 == 1 and count % 100 != 11
    return f"(по {count} оценённому)" if one else f"(по {count} оценённым)"


def work_line(label: str, fact: int, plan: int | None, was: int, tracked: bool) -> str:
    """Строка сделанной работы: «Размещений — 14 из 100 (+6)».

    Учёт не заведён — вместо числа так и сказано. Ноль с дельтой на этом
    месте был бы упрёком человеку, который ещё не начал заполнять лист.
    """
    if not tracked:
        return f"{label}{DASH}{NOT_TRACKED}"
    return f"{label}{DASH}{fact}{plan_tail(plan)}{change(fact, was)}"


def numbers_block(cur: WeekStats, prev: WeekStats) -> str:
    """Цифры недели обычными строками: по одной мысли на строку.

    Колонок и выравнивания здесь нет намеренно: ровнять их пришлось бы в
    <pre>, а он рисуется блоком кода с кнопкой «копировать» — сводку в
    таком виде неудобно читать с телефона.
    """
    lines = [
        work_line(
            "Размещений", cur.placements, cur.plan_placements,
            prev.placements, cur.placements_tracked,
        ),
        work_line(
            "Личных касаний", cur.outreach_sent, cur.plan_outreach,
            prev.outreach_sent, cur.outreach_tracked,
        ),
    ]

    if cur.responses == 0:
        # Пустая неделя — тоже результат, и сказано об этом прямо. Дельта
        # здесь не «минус столько-то», а «на прошлой неделе было»: читать
        # «откликов нет (−29)» пришлось бы дважды.
        was = f" (было {prev.responses})" if prev.responses else ""
        lines.append(NO_RESPONSES_LINE + was)
    else:
        lines.append(f"Откликов{DASH}{cur.responses}{change(cur.responses, prev.responses)}")
        lines.append("")
        lines.append(
            "Из них: "
            + DOT.join(f"{level} {cur.verdicts.get(level, 0)}" for level in ("A", "B", "C"))
        )
        if cur.scored and cur.average is not None:
            lines.append(
                f"Средний балл{DASH}{num(cur.average, 1)} {scored_note(cur.scored)}"
            )
        lines.append(f"Отсев на условиях{DASH}{cur.rejected}")

        sources = [
            f"каналы {cur.channel_responses}",
            f"личеки {cur.outreach_responses}",
        ]
        # Источники обязаны сходиться с общим числом откликов, иначе им
        # незачем верить: читатель первым делом сложит их в голове.
        if cur.unlabelled:
            sources.append(f"без метки {cur.unlabelled}")
        unknown = sum(cur.unknown_labels.values())
        if unknown:
            sources.append(f"чужая метка {unknown}")
        lines.append("Источники: " + DOT.join(sources))

    # Эти две строки печатаются, только если есть что сказать.
    tail = []
    if cur.empty_channels:
        tail.append("Пусто: " + listing(cur.empty_channels))
    if cur.silent_days:
        tail.append("Тишина: " + short_days(cur.silent_days))
    if tail:
        lines.append("")
        lines += tail

    return "\n".join(lines).strip("\n")


# --------------------- выводы, когда модель молчит ------------------------
#
# Раздел выводов не должен пустовать. Молчание читается как «сказать
# нечего», а сказать почти всегда есть что — и это видно из тех же цифр.
# Поэтому на случай недоступной модели здесь лежат выводы, собранные
# кодом: ни одного домысла, только то, что прямо следует из чисел.

# Что вносить в лист, по которому учёт ещё не начат.
TRACKING_HINTS = {
    PLACEMENTS_SHEET: "дату и канал каждого размещения",
    OUTREACH_SHEET: "дату и сколько сообщений отправлено",
}


def nika_fallback(cur: WeekStats) -> str:
    """Вывод для Ники по цифрам. Самое первое по важности — одной фразой."""
    if cur.untracked:
        what = "; ".join(
            f"«{title}» — {TRACKING_HINTS[title]}"
            for title in cur.untracked
            if title in TRACKING_HINTS
        )
        return f"Учёт не заведён. Заполните {what}. Ссылку для поста берите там же."
    if cur.unlabelled:
        return (
            f"Откликов без метки {cur.unlabelled}: ссылку раздают без метки. "
            "Берите готовую из колонки «Ссылка для размещения»."
        )
    if cur.empty_channels:
        return "Каналы без единого отклика: " + listing(cur.empty_channels) + "."
    if cur.idle_days:
        return "Дни без работы: " + short_days(cur.idle_days) + "."
    return ""


def vacancy_fallback(cur: WeekStats) -> str:
    """Вывод о вакансии по цифрам. Пустым не бывает: отсутствие откликов —
    тоже сведения об оффере."""
    if not cur.responses:
        return "Откликов нет — судить об оффере пока нечем."
    if cur.reasons:
        reason, count = max(cur.reasons.items(), key=lambda kv: (kv[1], kv[0]))
        return (
            f"Отсеялись на условиях {cur.rejected} из {cur.responses}, "
            f"чаще всего — {reason} ({count})."
        )
    if not cur.verdicts.get("A"):
        return "Сильных кандидатов нет: все отклики уровня B и C."
    return "Отклики идут и проходят по условиям — к вакансии вопросов нет."


def fit(text: str, budget: int) -> str:
    """Обрезка по слову с отбивкой. Не влезает ничего — пустая строка.

    Режутся выводы модели, и только они: цифры в сообщении неприкосновенны,
    из-за них отчёт и читают.
    """
    text = " ".join(str(text).split())
    if len(text) <= budget:
        return text
    if budget < 40:
        return ""
    cut = text[: budget - len(ELLIPSIS)]
    space = cut.rfind(" ")
    return (cut[:space] if space > 20 else cut).rstrip(" ,.;:") + ELLIPSIS


def section(title: str, text: str) -> str:
    return f"<b>{title}</b>\n{esc(text)}"


def message(
    cur: WeekStats,
    prev: WeekStats,
    nika: str = "",
    vacancy: str = "",
    limit: int = MESSAGE_LIMIT,
) -> str:
    """Готовое сообщение: заголовок, цифры, два раздела выводов.

    Потолок соблюдается за счёт выводов: сначала собираются цифры, потом в
    остаток бюджета вписывается текст модели — сперва «Нике», затем «По
    вакансии». Не влезло — раздела в сообщении нет.
    """
    head = f"<b>Подбор {DOT.strip()} {period_title(cur.period)}</b>"
    body = f"{head}\n\n{esc(numbers_block(cur, prev))}"

    for title, text in ((NIKA_TITLE, nika), (VACANCY_TITLE, vacancy)):
        text = " ".join(str(text or "").split())
        if not text:
            continue
        # Бюджет считается по готовому сообщению: экранирование удлиняет
        # текст, и считать его по сырому значило бы однажды выйти за потолок.
        scaffold = len(section(title, "")) + len("\n\n")
        room = min(SECTION_LIMIT, limit - len(body) - scaffold)
        text = fit(text, room)
        if text:
            body += "\n\n" + section(title, text)

    return body
