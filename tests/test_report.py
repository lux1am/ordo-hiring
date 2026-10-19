"""Еженедельный отчёт: цифры, текст, отправка, расписание.

Отчёт уходит раз в неделю, и проверить его «посмотрев в понедельник»
значит заметить ошибку через семь дней после того, как она появилась.
Поэтому недели здесь выдуманные, а цифры по ним считаются полностью: от
границ периода до конверсии личных касаний.

Главное, что эти тесты стерегут, — раздельный учёт источников. Каналы и
личные касания складывать нельзя: двадцать постов и двадцать сообщений в
личку это разная работа, и смысл отчёта в том, какая из них сработала.
"""
import io
import json
import logging
import re
import urllib.error
from datetime import date

import pytest
import yaml

import weekly_report
from HEADERS import HEADERS
from app import report, source
from app.config import PLAN_OUTREACH_DAY, PLAN_PLACEMENTS_DAY
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "report.yml"
PROMPT = (ROOT / "prompts" / "report.md").read_text(encoding="utf-8")

# Отчётная неделя тестов: 5–11 октября 2026 года, понедельник–воскресенье.
WEEK = report.Period(date(2026, 10, 5), date(2026, 10, 11))
BEFORE = WEEK.shifted(-1)


# ------------------------------- границы недели ---------------------------

@pytest.mark.parametrize(
    "today",
    [date(2026, 10, 12), date(2026, 10, 14), date(2026, 10, 18)],
    ids=["понедельник", "среда", "воскресенье"],
)
def test_report_week_is_the_previous_full_one(today):
    """Запуск в любой день даёт одну и ту же прошедшую неделю."""
    assert report.last_week(today) == WEEK


def test_week_is_seven_days_with_five_working():
    assert len(WEEK.days) == 7
    assert WEEK.working_days == 5
    assert WEEK.title() == "05.10–11.10"


def test_period_boundaries_are_inclusive():
    assert WEEK.start in WEEK and WEEK.end in WEEK
    assert date(2026, 10, 4) not in WEEK
    assert date(2026, 10, 12) not in WEEK
    assert None not in WEEK


# -------------------------------- разбор ячеек ----------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (46300, date(2026, 10, 5)),            # серийная дата Sheets
        ("2026-10-05", date(2026, 10, 5)),
        ("2026-10-05 14:03:22", date(2026, 10, 5)),
        ("05.10.2026", date(2026, 10, 5)),
        ("", None),
        (None, None),
        ("вторник", None),
    ],
)
def test_dates_are_read_from_whatever_the_cell_holds(value, expected):
    assert report.parse_date(value) == expected


def test_serial_date_matches_the_sheets_epoch():
    """День 0 в Sheets — 30 декабря 1899 года. Сдвиг на сутки увёл бы всю
    неделю."""
    assert report.parse_date(1) == date(1899, 12, 31)
    assert report.parse_date(45658) == date(2025, 1, 1)


def test_numbers_survive_a_comma_and_spaces():
    assert report.parse_int("20") == 20
    assert report.parse_int("2 0") == 20
    assert report.parse_int("мусор") == 0
    assert report.parse_float("8,5") == 8.5
    assert report.parse_float("") is None


# ------------------------------ чтение листов -----------------------------

def data_row(**over) -> list:
    row = [""] * len(HEADERS)
    values = {
        "id": "id001",
        "created_at": "2026-10-06 10:00:00",
        "verdict": "A",
        "total_score": 13,
        "source": "p002",
        # Проходные ответы на все стоп-вопросы: такая анкета доходит до баллов.
        "stop_1_sdelno": "Да, полностью готов",
        "stop_2_chasy": "20–24",
        "stop_3_svyaz": "Да — часовой пояс: МСК",
        "stop_4_oformlenie": "Самозанятость",
        "stop_5_tehnika": "Да, всё есть",
        "stop_6_zapis": "Да, полностью готов",
    }
    values.update(over)
    for name, value in values.items():
        row[HEADERS.index(name)] = value
    return row


def data_values(*rows: list) -> list[list]:
    return [HEADERS, *rows]


def placement_values(*rows: list) -> list[list]:
    return [["Дата", "Канал", "Ссылка", "Откликов"], *rows]


def outreach_values(*rows: list) -> list[list]:
    return [["Дата", "Отправлено", "Метка", "Ссылка", "Откликов"], *rows]


def read_placements(rows: list[list]) -> list:
    return report.read_placements(rows, date_col=1, channel_col=2)


def read_outreach(rows: list[list]) -> list:
    return report.read_outreach(rows, date_col=1, sent_col=2)


def test_responses_are_read_with_their_labels_and_verdicts():
    rows = data_values(data_row(), data_row(id="id002", verdict="C", source=""))
    responses = report.read_responses(rows)

    assert len(responses) == 2
    assert responses[0].day == date(2026, 10, 6)
    assert responses[0].label == "p002"
    assert responses[0].verdict == "A"
    assert responses[1].label == ""


def test_rows_without_id_are_not_responses():
    """Под шапкой лежит размеченная формулой пустота на триста строк."""
    empty = [""] * len(HEADERS)
    empty[HEADERS.index("total_score")] = 0
    assert report.read_responses(data_values(empty)) == []


def test_stop_failures_come_with_their_reasons():
    rows = data_values(data_row(stop_3_svyaz="Нет"))
    response = report.read_responses(rows)[0]

    assert response.rejected is True
    assert response.scored is False
    assert response.stop_reasons == ["связь в будни с 11:00 до 17:00 по МСК"]


def test_label_in_the_data_sheet_is_cleaned_before_use():
    """В колонку могла попасть метка в другом регистре — это та же метка."""
    rows = data_values(data_row(source="P002"))
    assert report.read_responses(rows)[0].label == "p002"


def test_placement_label_is_its_row_number():
    rows = placement_values(
        ["2026-10-05", "Авито", "", ""],
        ["", "", "", ""],
        ["2026-10-07", "Телеграм", "", ""],
    )
    posts = read_placements(rows)

    assert [p.label for p in posts] == [source.label(2), source.label(4)]
    assert [p.channel for p in posts] == ["Авито", "Телеграм"]
    assert posts[0].day == date(2026, 10, 5)


def test_outreach_label_is_built_from_the_date_not_read_from_the_sheet():
    """Колонка метки — формула. Отчёт не должен зависеть от того, успела ли
    таблица пересчитаться, поэтому метку он считает сам."""
    rows = outreach_values(["2026-10-06", 20, "ЧУЖОЕ", "", ""])
    touches = read_outreach(rows)

    assert touches[0].label == "lk_06_10"
    assert touches[0].sent == 20


# ------------------------------- подсчёт недели ---------------------------

@pytest.fixture
def week():
    """Неделя с размещениями в двух каналах и личными касаниями.

    Размещения: p002 и p003 — Авито, p004 — Телеграм.
    Отклики: два по p002, один по p003, один из личек, один без метки,
    один с меткой, которой нет ни у одного размещения.
    """
    placements_rows = placement_values(
        ["2026-10-05", "Авито", "", ""],
        ["2026-10-07", "Авито", "", ""],
        ["2026-10-07", "Телеграм", "", ""],
    )
    outreach_rows = outreach_values(
        ["2026-10-05", 20, "", "", ""],
        ["2026-10-06", 15, "", "", ""],
    )
    data = data_values(
        data_row(id="id01", created_at="2026-10-05 10:00:00", source="p002", verdict="A", total_score=13),
        data_row(id="id02", created_at="2026-10-06 10:00:00", source="p002", verdict="B", total_score=10),
        data_row(id="id03", created_at="2026-10-07 10:00:00", source="p003", verdict="C", total_score=4),
        data_row(id="id04", created_at="2026-10-07 11:00:00", source="lk_05_10", verdict="B", total_score=9),
        data_row(id="id05", created_at="2026-10-08 10:00:00", source="", verdict="C", total_score=3),
        data_row(id="id06", created_at="2026-10-09 10:00:00", source="p099", verdict="C", total_score=2),
        # Прошлая неделя — для дельты.
        data_row(id="id07", created_at="2026-09-30 10:00:00", source="p002", verdict="C", total_score=5),
        # Следующая неделя: в отчёт попасть не должна.
        data_row(id="id08", created_at="2026-10-13 10:00:00", source="p002", verdict="A", total_score=14),
    )
    responses = report.read_responses(data)
    posts = read_placements(placements_rows)
    touches = read_outreach(outreach_rows)
    return (
        report.stats(WEEK, responses, posts, touches),
        report.stats(BEFORE, responses, posts, touches),
    )


def test_responses_are_counted_by_their_own_week(week):
    current, previous = week
    assert current.responses == 6
    assert previous.responses == 1, "отклик прошлой недели остаётся в прошлой"


def test_sources_are_counted_separately(week):
    """Каналы, личные касания, без метки и чужая метка — четыре разных числа."""
    current, _ = week
    assert current.channel_responses == 3
    assert current.outreach_responses == 1
    assert current.unlabelled == 1
    assert current.unknown_labels == {"p099": 1}
    assert (
        current.channel_responses
        + current.outreach_responses
        + current.unlabelled
        + sum(current.unknown_labels.values())
        == current.responses
    ), "каждый отклик учтён ровно один раз"


def test_channel_conversion_is_per_placement(week):
    current, _ = week
    assert current.placements == 3
    assert current.per_placement == 1.0


def test_outreach_conversion_is_per_message_sent(week):
    current, _ = week
    assert current.outreach_sent == 35
    assert current.outreach_responses == 1
    assert round(current.outreach_rate, 4) == round(1 / 35, 4)


def test_conversions_never_divide_by_zero():
    empty = report.stats(WEEK, [], [], [])
    assert empty.per_placement is None
    assert empty.outreach_rate is None


def test_verdicts_and_average_are_counted(week):
    current, _ = week
    assert current.verdicts == {"A": 1, "B": 2, "C": 3}
    assert current.average == pytest.approx((13 + 10 + 4 + 9 + 3 + 2) / 6)


def test_average_ignores_those_cut_off_by_stop_questions():
    """Отсеявшийся до баллов не доходит: его ноль занижал бы средний балл."""
    data = data_values(
        data_row(id="id01", created_at="2026-10-05 10:00:00", total_score=12, verdict="A"),
        data_row(
            id="id02", created_at="2026-10-06 10:00:00", total_score=0,
            verdict="C", stop_3_svyaz="Нет",
        ),
    )
    current = report.stats(WEEK, report.read_responses(data), [], [])

    assert current.rejected == 1
    assert current.reasons == {"связь в будни с 11:00 до 17:00 по МСК": 1}
    assert current.average == 12


def test_channels_carry_their_week_and_their_history(week):
    current, _ = week
    by_name = {c.channel: c for c in current.channels}

    assert by_name["Авито"].placements == 2
    assert by_name["Авито"].responses == 3
    assert by_name["Телеграм"].placements == 1
    assert by_name["Телеграм"].responses == 0
    assert current.empty_channels == ["Телеграм"]


def test_fading_channel_is_seen_by_first_against_last():
    """Канал выдохся: первое размещение дало отклики, последнее — ноль."""
    posts = read_placements(
        placement_values(
            ["2026-09-28", "Авито", "", ""],   # p002 — три отклика
            ["2026-10-06", "Авито", "", ""],   # p003 — ни одного
        )
    )
    data = data_values(
        *[
            data_row(id=f"id{i}", created_at="2026-09-28 10:00:00", source="p002")
            for i in range(3)
        ]
    )
    current = report.stats(WEEK, report.read_responses(data), posts, [])
    channel = current.channels[0]

    assert channel.total_placements == 2
    assert channel.first_yield == 3
    assert channel.last_yield == 0
    assert channel.fading is True


def test_single_placement_channel_is_not_called_fading():
    posts = read_placements(placement_values(["2026-10-05", "Авито", "", ""]))
    current = report.stats(WEEK, [], posts, [])
    assert current.channels[0].fading is False


def test_silent_days_are_listed(week):
    current, _ = week
    # Отклики пришли 5, 6, 7, 8 и 9 октября — молчали суббота и воскресенье.
    assert current.silent_days == [date(2026, 10, 10), date(2026, 10, 11)]


def test_a_week_without_responses_is_silent_every_day():
    current = report.stats(WEEK, [], [], [])
    assert len(current.silent_days) == 7


def test_plan_comes_from_the_daily_rate():
    current = report.stats(WEEK, [], [], [])
    assert current.plan_placements == PLAN_PLACEMENTS_DAY * 5 == 100
    assert current.plan_outreach == PLAN_OUTREACH_DAY * 5 == 100


# -------------------------------- блок цифр -------------------------------

def test_numbers_text_states_plan_fact_and_deviation(week):
    current, previous = week
    text = report.numbers_text(current, previous)

    assert "План размещений: план 100, факт 3, отклонение −97" in text
    assert "План касаний: план 100, факт 35, отклонение −65" in text


def test_response_plan_appears_from_one_line_in_the_config(week):
    """Плана по откликам пока нет. Впишут число — строка появится сама."""
    current, previous = week
    assert "План по откликам" not in report.numbers_text(current, previous)

    from app import config

    before = config.PLAN_RESPONSES_WEEK
    try:
        config.PLAN_RESPONSES_WEEK = 10
        text = report.numbers_text(current, previous)
    finally:
        config.PLAN_RESPONSES_WEEK = before

    assert "План по откликам: план 10, факт 6, отклонение −4" in text


def test_daily_plan_is_the_only_place_to_change_the_weekly_one(monkeypatch):
    """Недельный план — дневной, умноженный на рабочие дни. Другого места,
    где его правят, нет."""
    from app import config

    monkeypatch.setattr(config, "PLAN_PLACEMENTS_DAY", 5)
    assert report.stats(WEEK, [], [], []).plan_placements == 25


def test_numbers_text_shows_the_delta_for_every_number(week):
    current, previous = week
    text = report.numbers_text(current, previous)

    assert "Откликов: 6 (было 1, +5)" in text
    assert "Откликов из каналов: 3 (было 1, +2)" in text
    assert "Откликов из личек: 1 (было 0, +1)" in text
    assert "было" in text.split("ПО ДНЯМ")[0]


def test_numbers_text_keeps_the_sources_apart(week):
    current, previous = week
    text = report.numbers_text(current, previous)

    assert "РАЗМЕЩЕНИЯ В КАНАЛАХ" in text and "ЛИЧНЫЕ КАСАНИЯ" in text
    assert "Конверсия личных касаний: 2,9%" in text
    assert "Откликов на размещение: 1,00" in text
    assert "Отклики без метки: 1" in text
    assert "p099 — 1" in text


def test_numbers_text_names_empty_and_fading_channels(week):
    current, previous = week
    text = report.numbers_text(current, previous)

    assert "Авито: размещений 2, откликов 3" in text
    assert "Каналы без единого отклика: Телеграм" in text
    assert "Каналы с падающей отдачей:" in text


def test_numbers_text_lists_silent_days_with_weekday_names(week):
    current, previous = week
    text = report.numbers_text(current, previous)
    assert "Дни без единого отклика: Сб 10.10, Вс 11.10" in text


def test_empty_week_says_there_are_no_responses():
    """Отчёт уходит и о пустой неделе — с явной строкой, без умолчаний."""
    current = report.stats(WEEK, [], [], [])
    previous = report.stats(BEFORE, [], [], [])
    text = report.numbers_text(current, previous)

    assert report.NO_RESPONSES_LINE in text
    assert "За неделю не размещались ни в одном канале" in text


def test_numbers_text_has_no_empty_sections():
    """Строка плана, которой нет, не должна оставлять за собой дырку."""
    text = report.numbers_text(
        report.stats(WEEK, [], [], []), report.stats(BEFORE, [], [], [])
    )
    assert "\n\n\n" not in text
    assert not text.endswith("\n")


def test_numbers_are_written_the_russian_way():
    assert report.num(0.325, 2) == "0,33"
    assert report.num(None) == "—"
    assert report.signed(-5) == "−5", "типографский минус, а не дефис"
    assert report.signed(0) == "без изменений"
    assert report.percent(0.0784) == "7,8%"
    assert report.percent(None) == "—"


# ------------------------------- сообщение --------------------------------
#
# Сообщение читают с телефона в понедельник утром, и форма у него жёсткая:
# заголовок, цифры обычными строками, два коротких раздела выводов. Потолок
# — 1500 знаков, и соблюдается он за счёт выводов, а не цифр.

@pytest.fixture
def filled():
    """Неделя 12–18 октября (Пн–Вс): три размещения, два дня личек."""
    posts = read_placements(
        placement_values(
            ["2026-10-14", "Авито", "", ""],
            ["2026-10-15", "Работа в продажах", "", ""],
            ["2026-10-16", "HR чат МСК", "", ""],
        )
    )
    touches = read_outreach(
        outreach_values(["2026-10-13", 30, "", "", ""], ["2026-10-16", 32, "", "", ""])
    )
    rows = [
        data_row(id="a1", created_at="2026-10-16 10:00:00", source="p002",
                 verdict="A", total_score=13),
        *[
            data_row(id=f"b{i}", created_at="2026-10-16 11:00:00", source="p002",
                     verdict="B", total_score=9)
            for i in range(4)
        ],
        *[
            data_row(id=f"c{i}", created_at="2026-10-17 10:00:00", source="lk_13_10",
                     verdict="C", total_score=4)
            for i in range(6)
        ],
        data_row(id="s1", created_at="2026-10-18 10:00:00", source="p002",
                 verdict="C", total_score=0, stop_1_sdelno="Нет, нужен оклад"),
    ]
    responses = report.read_responses(data_values(*rows))

    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, responses, posts, touches)
    previous = report.stats(week.shifted(-1), responses, posts, touches)
    # Прошлая неделя подделана: нужна дельта в обе стороны.
    previous.placements, previous.outreach_sent, previous.responses = 9, 58, 17
    return current, previous


def test_message_follows_the_agreed_shape(filled):
    current, previous = filled
    text = report.message(current, previous, "Вывод Нике.", "Вывод по вакансии.")

    assert text.startswith("<b>Подбор · 12–18 октября</b>\n\nРазмещений — ")
    assert "<b>Нике</b>\nВывод Нике." in text
    assert "<b>По вакансии</b>\nВывод по вакансии." in text
    # Цифры выше выводов: их читают первыми.
    assert text.index("Откликов — ") < text.index("<b>Нике</b>")


def test_message_has_no_code_blocks(filled):
    """<pre> и <code> Телеграм рисует блоком кода с кнопкой «копировать»:
    сводка выглядит технической выгрузкой и читается хуже."""
    current, previous = filled
    text = report.message(current, previous, "вывод", "вывод")

    for tag in ("<pre>", "<code>", "```"):
        assert tag not in text, tag
    # Из разметки остаются только жирные заголовки разделов.
    assert set(re.findall(r"</?([a-z]+)>", text)) == {"b"}


def test_numbers_go_line_by_line(filled):
    current, previous = filled
    lines = report.numbers_block(current, previous).split("\n")

    assert lines[0] == "Размещений — 3 из 100 (−6)"
    assert lines[1] == "Личных касаний — 62 из 100 (+4)"
    assert lines[2] == "Откликов — 12 (−5)"
    assert lines[3] == ""
    assert lines[4] == "Из них: A 1 · B 4 · C 7"
    assert "Источники: каналы 6 · личеки 6" in lines


def test_delta_shows_up_only_where_there_is_one(filled):
    current, previous = filled
    previous.outreach_sent = current.outreach_sent      # без изменений
    previous.responses = None                           # сравнивать нечего
    lines = report.numbers_block(current, previous).split("\n")

    assert lines[1] == "Личных касаний — 62 из 100", "нулевая дельта — шум"
    assert lines[2] == "Откликов — 12"
    assert report.change(5, None) == ""
    assert report.change(5, 5) == ""
    assert report.change(14, 20) == " (−6)"
    assert report.change(20, 14) == " (+6)"


def test_average_says_how_many_it_covers(filled):
    """Средний балл считается только по оценённым, а читается как среднее по
    всем — отсюда и подпись в скобках."""
    current, previous = filled
    block = report.numbers_block(current, previous)

    assert current.scored == 11
    assert "Средний балл — 6,6 (по 11 оценённым)" in block
    assert "Отсев на условиях — 1" in block


def test_average_line_is_absent_when_nobody_was_scored(filled):
    current, previous = filled
    current.scored, current.average = 0, None
    assert "Средний балл" not in report.numbers_block(current, previous)


@pytest.mark.parametrize(
    "count,text",
    [(1, "(по 1 оценённому)"), (2, "(по 2 оценённым)"), (5, "(по 5 оценённым)"),
     (11, "(по 11 оценённым)"), (21, "(по 21 оценённому)")],
)
def test_scored_note_is_declined(count, text):
    assert report.scored_note(count) == text


def test_sources_in_brackets_add_up_to_the_total(filled):
    """Скобки обязаны сходиться с общим числом откликов.

    Иначе им незачем верить: читатель первым делом сложит их в голове.
    """
    current, previous = filled
    assert current.responses == 12
    line = next(
        l for l in report.numbers_block(current, previous).split("\n")
        if l.startswith("Источники:")
    )
    assert sum(int(n) for n in re.findall(r"\d+", line)) == 12

    # Отклики без метки и с чужой меткой показываются, только когда есть.
    assert "без метки" not in line
    current.unlabelled = 2
    current.unknown_labels = {"p099": 1}
    current.responses += 3
    line = next(
        l for l in report.numbers_block(current, previous).split("\n")
        if l.startswith("Источники:")
    )

    assert "без метки 2" in line and "чужая метка 1" in line
    assert sum(int(n) for n in re.findall(r"\d+", line)) == 15


def test_quiet_sections_are_printed_only_when_there_is_something_to_say(filled):
    current, previous = filled
    block = report.numbers_block(current, previous)
    assert "Пусто: HR чат МСК, Работа в продажах" in block
    assert "Тишина: 12, 13, 14, 15 окт" in block

    # Все каналы отработали и откликов не было только в один день — строк нет.
    current.channels = []
    current.days = [report.DayRow(day=d, responses=1) for d in current.period.days]
    quiet = report.numbers_block(current, previous)
    assert "Пусто:" not in quiet and "Тишина:" not in quiet


def test_long_channel_list_does_not_eat_the_message(filled):
    current, previous = filled
    current.channels = [
        report.ChannelRow(channel=f"Канал {i}", placements=1, responses=0)
        for i in range(12)
    ]
    block = report.numbers_block(current, previous)
    assert "и ещё 7" in block
    assert len(block.split("Пусто: ")[1].split("\n")[0]) < 120


def test_empty_week_says_it_plainly():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, [], [], [])
    previous = report.stats(week.shifted(-1), [], [], [])
    text = report.message(current, previous)

    assert report.NO_RESPONSES_LINE in text
    assert "Тишина: 12, 13, 14, 15, 16, 17, 18 окт" in text
    # Ни «Из них», ни «Источники», ни «Отсев»: считать нечего.
    for absent in ("Из них:", "Источники:", "Отсев"):
        assert absent not in text, absent
    assert "\n\n\n" not in text, "пустая строка не должна удваиваться"


def test_empty_week_recalls_the_previous_one():
    """«Откликов нет (−29)» читалось бы дважды — здесь нужен прошлый итог."""
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, [], [], [])
    previous = report.stats(week.shifted(-1), [], [], [])
    previous.responses = 29

    assert "Откликов нет (было 29)" in report.numbers_block(current, previous)
    previous.responses = 0
    block = report.numbers_block(current, previous)
    assert "Откликов нет\n" in block + "\n" and "было" not in block


# ------------------------ учёт ещё не заведён -----------------------------
#
# Пустой лист за всю историю и пустая неделя — разные вещи. В первом случае
# ноль означает, что таблицу ещё не начали заполнять, и упрекать человека
# нечем; во втором — что работа не сделана. Путать их нельзя.

def test_empty_sheets_mean_tracking_has_not_started():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    nothing = report.stats(week, [], [], [])

    assert nothing.placements_tracked is False
    assert nothing.outreach_tracked is False
    assert nothing.tracking_started is False
    assert nothing.untracked == ["Размещения", "Личные касания"]


def test_a_single_row_anywhere_means_tracking_has_started():
    """Строка с прошлого месяца — уже учёт: ноль за эту неделю настоящий."""
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    posts = read_placements(placement_values(["2026-08-01", "Авито", "", ""]))
    touches = read_outreach(outreach_values(["2026-08-01", 5, "", "", ""]))

    started = report.stats(week, [], posts, touches)
    assert started.tracking_started is True
    assert started.untracked == []
    assert started.placements == 0, "за неделю размещений не было — и это правда"


def test_row_without_a_date_still_counts_as_tracking():
    """Канал заведён, дату не поставили — таблицу уже начали заполнять."""
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    posts = read_placements(placement_values(["", "Авито", "", ""]))
    assert report.stats(week, [], posts, []).placements_tracked is True


def test_untracked_work_is_not_reported_as_zero():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, [], [], [])
    previous = report.stats(week.shifted(-1), [], [], [])
    lines = report.numbers_block(current, previous).split("\n")

    assert lines[0] == f"Размещений — {report.NOT_TRACKED}"
    assert lines[1] == f"Личных касаний — {report.NOT_TRACKED}"
    assert "из 100" not in "\n".join(lines[:2]), "плана там, где нет учёта, нет"
    assert "(+0)" not in "\n".join(lines) and "(−0)" not in "\n".join(lines)


def test_one_untracked_sheet_does_not_hide_the_other():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    posts = read_placements(placement_values(["2026-10-14", "Авито", "", ""]))
    current = report.stats(week, [], posts, [])
    previous = report.stats(week.shifted(-1), [], posts, [])
    lines = report.numbers_block(current, previous).split("\n")

    assert lines[0] == "Размещений — 1 из 100 (+1)", "здесь учёт есть"
    assert lines[1] == f"Личных касаний — {report.NOT_TRACKED}"
    assert current.untracked == ["Личные касания"]
    assert current.tracking_started is False, "учёт заведён не весь"


def test_model_gets_the_tracking_flag_explicitly():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, [], [], [])
    previous = report.stats(week.shifted(-1), [], [], [])
    facts = report.numbers_text(current, previous)

    assert "tracking_started: false" in facts
    assert "пусты за всю историю" in facts
    assert "отсутствие учёта, а не отсутствие работы" in facts
    assert f"Размещений: {report.NOT_TRACKED}" in facts
    assert f"Отправлено: {report.NOT_TRACKED}" in facts
    # Плана там, где учёта нет, тоже быть не должно: сравнивать нечего.
    assert "План размещений" not in facts
    assert "План касаний" not in facts


def test_started_tracking_is_flagged_as_true():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    posts = read_placements(placement_values(["2026-10-14", "Авито", "", ""]))
    touches = read_outreach(outreach_values(["2026-10-14", 20, "", "", ""]))
    current = report.stats(week, [], posts, touches)
    facts = report.numbers_text(current, report.stats(week.shifted(-1), [], posts, touches))

    assert "tracking_started: true" in facts
    assert "пусты за всю историю" not in facts
    assert "План размещений: план 100, факт 1" in facts


def test_day_table_does_not_claim_idle_days_without_tracking():
    """«Дни без работы» по пустому листу — упрёк за то, чего не измеряли."""
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, [], [], [])
    facts = report.numbers_text(current, report.stats(week.shifted(-1), [], [], []))

    assert "Дни без работы" not in facts
    assert "размещений 0" not in facts and "касаний 0" not in facts
    assert "Пн 12.10: откликов 0" in facts, "отклики считаются всегда"


def test_day_table_is_full_when_tracking_works():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    posts = read_placements(placement_values(["2026-10-14", "Авито", "", ""]))
    touches = read_outreach(outreach_values(["2026-10-14", 20, "", "", ""]))
    current = report.stats(week, [], posts, touches)
    facts = report.numbers_text(current, report.stats(week.shifted(-1), [], posts, touches))

    assert "Ср 14.10: размещений 1, касаний 20, откликов 0" in facts
    assert "Дни без работы" in facts


def test_average_in_the_model_block_names_its_base():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, [], [], [])
    current.scored, current.average, current.responses = 5, 9.0, 13
    facts = report.numbers_text(current, report.stats(week.shifted(-1), [], [], []))

    assert "Средний балл: 9,0 по 5 оценённым" in facts
    assert ") (" not in facts, "две скобки подряд читать невозможно"


def test_prompt_forbids_judging_work_without_tracking():
    assert "tracking_started" in PROMPT
    assert "Не делай выводов о работе человека" in PROMPT
    assert "учёт ещё не начат" in PROMPT
    # Прежнее правило «работа не велась» должно остаться только для
    # заведённого учёта — иначе модель упрекнёт человека за пустую таблицу.
    assert "Размещений нет, а учёт заведён" in PROMPT


def test_prompt_warns_about_the_average_base():
    assert "по сколькам" in PROMPT


def test_message_never_goes_over_the_limit(filled):
    """Потолок жёсткий: обрезаются выводы модели, а не цифры."""
    current, previous = filled
    long_text = "слово " * 400
    text = report.message(current, previous, long_text, long_text)

    assert len(text) <= report.MESSAGE_LIMIT
    numbers = report.numbers_block(current, previous)
    assert report.esc(numbers) in text, "цифры должны остаться целиком"
    assert report.ELLIPSIS in text, "обрезанный вывод помечается отбивкой"


def test_runaway_first_section_does_not_starve_the_second(filled):
    """У каждого раздела свой предел: промпт просит две фразы, но
    рассчитывать на послушание модели нельзя."""
    current, previous = filled
    text = report.message(current, previous, "слово " * 400, "слово " * 400)

    assert len(text) <= report.MESSAGE_LIMIT
    assert "<b>Нике</b>" in text and "<b>По вакансии</b>" in text
    for part in text.split("<b>")[2:]:
        body = part.split("</b>\n")[1]
        assert len(body) <= report.SECTION_LIMIT, body[:60]


def test_section_is_dropped_when_there_is_no_room_at_all(filled):
    current, previous = filled
    tight = len(report.message(current, previous)) + 50
    text = report.message(current, previous, "слово " * 50, "слово " * 50, limit=tight)

    assert len(text) <= tight
    assert "<b>По вакансии</b>" not in text, "на второй раздел места не осталось"


def test_long_channel_names_are_shortened():
    """Названия заводит человек, и они бывают длиной в полстроки."""
    assert report.short_name("Авито") == "Авито"
    long = report.short_name("Телеграм-канал «Работа в продажах Москва и область»")
    assert len(long) <= report.CHANNEL_NAME_LIMIT
    assert long.endswith(report.ELLIPSIS)


def test_empty_conclusions_leave_no_empty_headers(filled):
    current, previous = filled
    text = report.message(current, previous, "", "   ")
    assert "<b>Нике</b>" not in text and "<b>По вакансии</b>" not in text


def test_outside_text_is_escaped_for_html(filled):
    """Одна угловая скобка в названии канала вернула бы ошибку 400."""
    current, previous = filled
    current.channels = [
        report.ChannelRow(channel="Работа <для> всех & ещё", placements=1, responses=0)
    ]
    text = report.message(current, previous, "жадность <b>не</b> пройдёт")

    assert "&lt;для&gt;" in text and "&amp;" in text
    assert "<b>не</b>" not in text
    # Разметка остаётся только та, которую поставил код.
    assert text.count("<b>") == text.count("</b>") == 2


def test_period_title_names_the_month():
    assert report.period_title(report.Period(date(2026, 10, 13), date(2026, 10, 19))) == (
        "13–19 октября"
    )
    assert report.period_title(report.Period(date(2026, 9, 28), date(2026, 10, 4))) == (
        "28 сентября – 4 октября"
    )


def test_short_days_name_the_month_once():
    assert report.short_days([date(2026, 10, 14), date(2026, 10, 15)]) == "14, 15 окт"
    assert report.short_days([date(2026, 9, 30), date(2026, 10, 1)]) == "30 сен, 1 окт"
    assert report.short_days([]) == ""


def test_fit_cuts_by_word_and_gives_up_when_there_is_no_room():
    assert report.fit("одно слово", 100) == "одно слово"
    cut = report.fit("слово " * 50, 40)
    assert len(cut) <= 40 and cut.endswith(report.ELLIPSIS)
    assert " " in cut and not cut.endswith(" " + report.ELLIPSIS)
    assert report.fit("слово " * 50, 20) == "", "в двадцать знаков вывод не влезет"


def test_build_message_fills_the_sections_itself_when_asked_to(filled):
    current, previous = filled
    text = weekly_report.build_message(current, previous)

    assert len(text) <= report.MESSAGE_LIMIT
    assert "<b>Нике</b>" in text and "<b>По вакансии</b>" in text


# -------------------------------- отправка --------------------------------

class FakeTelegram:
    """Bot API в памяти: кому ушло, что ушло и кто отказал."""

    def __init__(self, failing: set[str] = frozenset()):
        self.failing = failing
        self.sent: list[dict] = []

    def post(self, method: str, payload: dict) -> dict:
        assert method == "sendMessage"
        if payload["chat_id"] in self.failing:
            return {"ok": False, "description": "chat not found"}
        self.sent.append(payload)
        return {"ok": True}

    @property
    def chats(self) -> list[str]:
        return [p["chat_id"] for p in self.sent]


def recipients(*raw: str) -> list:
    return [
        weekly_report.Recipient(*(r.split(":")[0], int(r.split(":")[1]))) 
        if ":" in r else weekly_report.Recipient(r)
        for r in raw
    ]


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(
        weekly_report, "telegram_recipients",
        lambda: recipients("111", "-100222"),
    )
    return fake


def test_report_goes_to_every_recipient(telegram):
    sent, failed = weekly_report.broadcast("отчёт")
    assert (sent, failed) == (2, 0)
    assert telegram.chats == ["111", "-100222"]


def test_one_bad_recipient_does_not_stop_the_others(monkeypatch):
    fake = FakeTelegram(failing={"111"})
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(
        weekly_report, "telegram_recipients", lambda: recipients("111", "222", "333")
    )

    sent, failed = weekly_report.broadcast("отчёт")
    assert (sent, failed) == (2, 1)
    assert fake.chats == ["222", "333"]


def test_network_failure_of_one_recipient_is_isolated(monkeypatch):
    calls = []

    def post(method, payload):
        calls.append(payload["chat_id"])
        if payload["chat_id"] == "111":
            raise OSError("обрыв связи")
        return {"ok": True}

    monkeypatch.setattr(weekly_report, "post", post)
    monkeypatch.setattr(
        weekly_report, "telegram_recipients", lambda: recipients("111", "222")
    )

    assert weekly_report.broadcast("отчёт") == (1, 1)
    assert calls == ["111", "222"]


def test_message_is_sent_as_html(telegram):
    """Без parse_mode Телеграм показал бы теги заголовков как текст."""
    weekly_report.broadcast("<b>Подбор</b>")
    assert telegram.sent[0]["parse_mode"] == "HTML"
    assert telegram.sent[0]["text"] == "<b>Подбор</b>"
    assert telegram.sent[0]["disable_web_page_preview"] is True


# ------------------------- тема супергруппы -------------------------------

def test_topic_goes_into_the_request_body(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(
        weekly_report, "telegram_recipients",
        lambda: recipients("-1004367079334:12", "111"),
    )

    assert weekly_report.broadcast("отчёт") == (2, 0)
    forum, personal = fake.sent
    assert forum["chat_id"] == "-1004367079334"
    assert forum["message_thread_id"] == 12, "отчёт должен падать в свою тему"
    assert "message_thread_id" not in personal, "в личке тем нет"


def test_thread_id_is_a_number_in_the_payload():
    """Bot API ждёт message_thread_id числом: строка «12» даёт 400."""
    body = weekly_report.payload_for(
        weekly_report.Recipient("-100", 12), "текст"
    )
    assert body["message_thread_id"] == 12
    assert isinstance(body["message_thread_id"], int)


# --------------- отказ из-за темы: откат в общий поток --------------------
#
# Неверный или закрытый номер темы обнаруживается ровно в понедельник
# утром, когда править его некому. Отчёт в общем потоке хуже отчёта в своей
# ветке, но несравнимо лучше отчёта, которого нет.

THREAD_ERRORS = [
    "Bad Request: message thread not found",
    "Bad Request: TOPIC_CLOSED",
    "Bad Request: TOPIC_DELETED",
    "Bad Request: message_thread_id is invalid",
]


class TopicRefusing:
    """Bot API, который отказывает всему, что приходит с темой."""

    def __init__(self, description: str, http: bool = False, fail_plain: bool = False):
        self.description = description
        self.http = http               # отказ приходит исключением, а не телом
        self.fail_plain = fail_plain   # общий поток тоже не принимает
        self.payloads: list[dict] = []

    def post(self, method: str, payload: dict) -> dict:
        self.payloads.append(payload)
        if "message_thread_id" in payload:
            return self.refuse()
        if self.fail_plain:
            return {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        return {"ok": True}

    def refuse(self):
        if not self.http:
            return {"ok": False, "error_code": 400, "description": self.description}
        body = json.dumps(
            {"ok": False, "error_code": 400, "description": self.description}
        ).encode("utf-8")
        raise urllib.error.HTTPError(
            "https://api.telegram.org/botX/sendMessage", 400, self.description,
            {}, io.BytesIO(body),
        )


@pytest.mark.parametrize("description", THREAD_ERRORS)
def test_refused_topic_falls_back_to_the_common_thread(monkeypatch, caplog, description):
    fake = TopicRefusing(description)
    monkeypatch.setattr(weekly_report, "post", fake.post)

    with caplog.at_level(logging.WARNING):
        assert weekly_report.send_to(
            weekly_report.Recipient("-1004367079334", 12), "отчёт"
        ) is True

    assert len(fake.payloads) == 2, "одна попытка с темой, одна без"
    assert fake.payloads[0]["message_thread_id"] == 12
    assert "message_thread_id" not in fake.payloads[1]
    assert fake.payloads[1]["chat_id"] == "-1004367079334"
    assert fake.payloads[1]["text"] == "отчёт", "текст тот же, без изменений"

    warnings = " ".join(r.getMessage() for r in caplog.records)
    assert "повторяю без неё" in warnings
    assert "общий поток" in warnings
    assert "TELEGRAM_CHAT_IDS" in warnings, "в логе должно быть сказано, что править"


def test_topic_refusal_as_http_error_also_falls_back(monkeypatch):
    """Телеграм отвечает на 400 исключением HTTPError, а не телом ответа."""
    fake = TopicRefusing("Bad Request: message thread not found", http=True)
    monkeypatch.setattr(weekly_report, "post", fake.post)

    assert weekly_report.send_to(
        weekly_report.Recipient("-100", 7), "отчёт"
    ) is True
    assert len(fake.payloads) == 2


def test_other_refusals_are_not_retried(monkeypatch):
    """«Чата нет» темой не лечится — повтор только сжёг бы лимит."""
    fake = TopicRefusing("Bad Request: chat not found")

    def post(method, payload):
        fake.payloads.append(payload)
        return {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}

    monkeypatch.setattr(weekly_report, "post", post)

    assert weekly_report.send_to(weekly_report.Recipient("-100", 7), "отчёт") is False
    assert len(fake.payloads) == 1


def test_fallback_failure_is_still_a_failure(monkeypatch):
    fake = TopicRefusing("Bad Request: TOPIC_CLOSED", fail_plain=True)
    monkeypatch.setattr(weekly_report, "post", fake.post)

    assert weekly_report.send_to(weekly_report.Recipient("-100", 7), "отчёт") is False
    assert len(fake.payloads) == 2, "попробовать общий поток всё равно нужно"


def test_recipient_without_a_topic_has_nothing_to_fall_back_to(monkeypatch):
    calls = []

    def post(method, payload):
        calls.append(payload)
        return {"ok": False, "error_code": 400, "description": "Bad Request: topic closed"}

    monkeypatch.setattr(weekly_report, "post", post)

    assert weekly_report.send_to(weekly_report.Recipient("111"), "отчёт") is False
    assert len(calls) == 1, "снимать нечего — повторять незачем"


def test_fallback_does_not_stop_the_rest_of_the_broadcast(monkeypatch):
    """Группа с неверной темой не должна мешать остальным получателям."""
    fake = TopicRefusing("Bad Request: message thread not found")
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(
        weekly_report, "telegram_recipients",
        lambda: recipients("-100222:5", "111"),
    )

    assert weekly_report.broadcast("отчёт") == (2, 0)
    assert [p["chat_id"] for p in fake.payloads] == ["-100222", "-100222", "111"]


@pytest.mark.parametrize(
    "error,code,topic",
    [
        ("Bad Request: message thread not found", 400, True),
        ("Bad Request: TOPIC_CLOSED", 400, True),
        ("Bad Request: chat not found", 400, False),
        ("Bad Request: message thread not found", 403, False),
        ("Bad Request: message is too long", 400, False),
        ("", 400, False),
    ],
)
def test_topic_error_is_recognised_by_code_and_wording(error, code, topic):
    assert weekly_report.is_topic_error(error, code) is topic


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("111", [("111", None)]),
        ("-100456", [("-100456", None)]),
        ("-1004367079334:12", [("-1004367079334", 12)]),
        (" 111 , -1004367079334:12 ", [("111", None), ("-1004367079334", 12)]),
        ("111,,", [("111", None)]),
        ("", []),
        ("   ", []),
        # Опечатка в номере темы не отменяет получателя: отчёт уйдёт в общий
        # поток группы, а не пропадёт.
        ("-100777:abc", [("-100777", None)]),
        ("-100777:", [("-100777", None)]),
        # Кусок без чата — не получатель.
        (":12", []),
        ("-100888:12:34", [("-100888", None)]),
    ],
)
def test_recipients_are_parsed_from_one_variable(monkeypatch, raw, expected):
    from app import config

    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS_RAW", raw)
    assert [tuple(r) for r in config.telegram_recipients()] == expected


def test_recipient_keeps_its_fields_named():
    from app import config

    r = config.Recipient("-100", 12)
    assert (r.chat_id, r.thread_id) == ("-100", 12)
    assert config.Recipient("111").thread_id is None


# -------------------------------- точка входа -----------------------------

@pytest.fixture
def collected(monkeypatch):
    """Подменяет чтение книги: цифры берутся из выдуманной недели."""
    current = report.stats(WEEK, [], [], [])
    previous = report.stats(BEFORE, [], [], [])
    numbers = report.numbers_text(current, previous)
    monkeypatch.setattr(
        weekly_report, "collect", lambda today: (current, previous, numbers)
    )
    monkeypatch.setattr(weekly_report, "now_local", lambda: __import__(
        "datetime").datetime(2026, 10, 12, 9, 0)
    )
    return numbers


def test_dry_run_prints_and_sends_nothing(collected, monkeypatch, capsys):
    def boom(*a, **kw):
        raise AssertionError("сухой прогон не должен ничего отправлять")

    monkeypatch.setattr(weekly_report, "post", boom)
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: [])

    assert weekly_report.main(["--dry-run", "--no-model"]) == 0
    printed = capsys.readouterr().out
    assert "<b>Подбор · 5–11 октября</b>" in printed, "печатается то, что уйдёт"
    assert report.NO_RESPONSES_LINE in printed
    # Сухой прогон показывает и то, что видела модель: иначе непонятно, из
    # каких чисел собран отчёт.
    assert "что получила модель" in printed
    assert "ПО ДНЯМ" in printed


def test_run_without_recipients_stops_before_counting(monkeypatch):
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: [])
    monkeypatch.setattr(weekly_report, "telegram_recipients", lambda: [])
    monkeypatch.setattr(weekly_report, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(
        weekly_report, "collect",
        lambda today: (_ for _ in ()).throw(AssertionError("считать было незачем")),
    )
    assert weekly_report.main([]) == 2


def test_missing_secrets_are_named(monkeypatch):
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: ["SHEET_ID"])
    assert weekly_report.main(["--dry-run"]) == 2


def test_report_is_sent_with_the_model_text(collected, monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: [])
    monkeypatch.setattr(
        weekly_report, "telegram_recipients", lambda: recipients("111")
    )
    monkeypatch.setattr(weekly_report, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(
        weekly_report, "conclusions",
        lambda numbers: weekly_report.Conclusions("ВЫВОД НИКЕ", "ВЫВОД ПО ВАКАНСИИ"),
    )

    assert weekly_report.main([]) == 0
    assert fake.chats == ["111"]
    text = fake.sent[0]["text"]
    assert "<b>Нике</b>\nВЫВОД НИКЕ" in text
    assert "<b>По вакансии</b>\nВЫВОД ПО ВАКАНСИИ" in text
    assert report.NO_RESPONSES_LINE in text


def test_failed_model_does_not_cancel_the_report(collected, monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: [])
    monkeypatch.setattr(
        weekly_report, "telegram_recipients", lambda: recipients("111")
    )
    monkeypatch.setattr(weekly_report, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(weekly_report, "ANTHROPIC_API_KEY", "ключ")
    monkeypatch.setattr(
        weekly_report, "client",
        lambda: (_ for _ in ()).throw(RuntimeError("модель недоступна")),
    )

    assert weekly_report.main([]) == 0
    text = fake.sent[0]["text"]
    assert report.NO_RESPONSES_LINE in text, "цифры уходят и без модели"
    assert weekly_report.API_FAILED_NOTE in text
    # Раздел не пустует: его заполнил код по тем же цифрам.
    assert "Откликов нет — судить об оффере пока нечем." in text
    assert "<b>Нике</b>" in text


def test_everyone_failing_is_an_error(collected, monkeypatch):
    fake = FakeTelegram(failing={"111"})
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: [])
    monkeypatch.setattr(
        weekly_report, "telegram_recipients", lambda: recipients("111")
    )
    monkeypatch.setattr(weekly_report, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(
        weekly_report, "conclusions", lambda numbers: weekly_report.Conclusions()
    )

    assert weekly_report.main([]) == 1


# ----------------------------- выводы модели ------------------------------

class FakeAnthropic:
    """Клиент, отдающий заранее заданные ответы по порядку. Сети не требует."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.answers.pop(0) if self.answers else "{}"
        block = type("Block", (), {"type": "text", "text": text})()
        return type("Response", (), {"content": [block]})()


def ask(monkeypatch, *answers: str):
    fake = FakeAnthropic(*answers)
    monkeypatch.setattr(weekly_report, "client", lambda: fake)
    monkeypatch.setattr(weekly_report, "ANTHROPIC_API_KEY", "ключ")
    return weekly_report.conclusions("ЦИФРЫ"), fake


def test_conclusions_come_as_two_sections(monkeypatch):
    said, fake = ask(
        monkeypatch, '{"nika": "Замени два пустых канала.", "vacancy": "Все C."}'
    )
    assert said.nika == "Замени два пустых канала."
    assert said.vacancy == "Все C."
    assert said.note == "", "модель ответила — объясняться незачем"
    assert len(fake.calls) == 1, "второй обход был бы потраченными деньгами"

    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["messages"] == [{"role": "user", "content": "ЦИФРЫ"}]
    assert "ни одного числа от себя" in call["system"].lower()


def test_conclusions_survive_a_markdown_wrapper(monkeypatch):
    """Модель иногда оборачивает JSON в ```json — это не повод терять отчёт."""
    said, _ = ask(monkeypatch, '```json\n{"nika": "А", "vacancy": "Б"}\n```')
    assert (said.nika, said.vacancy) == ("А", "Б")


def test_empty_section_is_asked_again(monkeypatch):
    """Пустой раздел — не ответ. На него уходит одна повторная просьба."""
    said, fake = ask(
        monkeypatch,
        '{"nika": "", "vacancy": "Все отклики уровня C."}',
        '{"nika": "Заполни «Размещения».", "vacancy": "Все отклики уровня C."}',
    )

    assert len(fake.calls) == 2
    assert said.nika == "Заполни «Размещения»."
    assert said.vacancy == "Все отклики уровня C."
    assert said.note == "", "со второй попытки модель ответила — это её выводы"

    # Повторная просьба идёт той же беседой: модель видит свой ответ и прямое
    # указание заполнить оба раздела.
    messages = fake.calls[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "ЦИФРЫ"
    assert messages[2]["content"] == weekly_report.INSIST
    assert "заполни оба" in weekly_report.INSIST.lower()


def test_retry_does_not_lose_the_section_that_was_filled(monkeypatch):
    """Во второй раз модель может промолчать уже по другому разделу."""
    said, fake = ask(
        monkeypatch,
        '{"nika": "Первый вывод.", "vacancy": ""}',
        '{"nika": "", "vacancy": "Второй вывод."}',
    )
    assert (said.nika, said.vacancy) == ("Первый вывод.", "Второй вывод.")
    assert said.note == ""


def test_still_empty_after_the_retry_is_reported(monkeypatch):
    said, fake = ask(
        monkeypatch, '{"nika": "", "vacancy": "Б"}', '{"nika": "", "vacancy": "Б"}'
    )
    assert len(fake.calls) == 2, "больше двух обходов не делаем"
    assert said.nika == ""
    assert said.note == weekly_report.EMPTY_ANSWER_NOTE


def test_unparsable_answer_is_logged_verbatim(monkeypatch, caplog):
    """Без дословного ответа «разбор не удался» нельзя отличить от «модель
    ответила не то», и диагностика упирается в догадки."""
    with caplog.at_level(logging.ERROR):
        said, fake = ask(monkeypatch, "я не умею в JSON")

    assert said.nika == "" and said.vacancy == ""
    assert said.note == weekly_report.API_FAILED_NOTE
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "я не умею в JSON" in logged
    assert "не разобрался как JSON" in logged


def test_api_failure_is_told_apart_from_an_empty_answer(monkeypatch):
    """«Выводы собрать не удалось» одинаково означало бы и пустой кошелёк, и
    молчание модели — а лечатся они по-разному."""
    def boom():
        raise RuntimeError("Your credit balance is too low")

    monkeypatch.setattr(weekly_report, "client", boom)
    monkeypatch.setattr(weekly_report, "ANTHROPIC_API_KEY", "ключ")

    said = weekly_report.conclusions("ЦИФРЫ")
    assert said.note == weekly_report.API_FAILED_NOTE
    assert weekly_report.API_FAILED_NOTE != weekly_report.EMPTY_ANSWER_NOTE


def test_model_is_not_called_without_a_key(monkeypatch):
    fake = FakeAnthropic('{"nika": "А"}')
    monkeypatch.setattr(weekly_report, "client", lambda: fake)
    monkeypatch.setattr(weekly_report, "ANTHROPIC_API_KEY", "")

    said = weekly_report.conclusions("ЦИФРЫ")
    assert said == weekly_report.Conclusions(note=weekly_report.NO_KEY_NOTE)
    assert fake.calls == [], "без ключа обращаться незачем"


# ------------------ выводы, собранные кодом ------------------------------
#
# Молчание модели не должно оставлять в отчёте пустой раздел: «сказать
# нечего» читается как вывод, которым оно не является.

def test_both_sections_are_filled_when_tracking_has_not_started():
    """Главный случай: учёта нет, модель молчит — разделы всё равно есть."""
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    rows = [
        data_row(id=f"c{i}", created_at="2026-10-14 10:00:00", source="",
                 verdict="C", total_score=0, stop_1_sdelno="Нет, нужен оклад")
        for i in range(8)
    ]
    current = report.stats(week, report.read_responses(data_values(*rows)), [], [])
    previous = report.stats(week.shifted(-1), [], [], [])
    assert current.tracking_started is False

    text = weekly_report.build_message(
        current, previous, weekly_report.Conclusions(note="причина")
    )

    nika = text.split("<b>Нике</b>\n")[1].split("\n\n")[0]
    vacancy = text.split("<b>По вакансии</b>\n")[1].split("\n\n")[0]
    assert nika.strip() and vacancy.strip(), "пустых разделов быть не должно"
    # В разделе Нике сказано, какие листы пусты и что в них внести.
    for title in ("Размещения", "Личные касания"):
        assert title in nika, title
    assert "дату" in nika
    assert "Отсеялись на условиях" in vacancy


def test_code_written_sections_are_marked_as_such():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, [], [], [])
    previous = report.stats(week.shifted(-1), [], [], [])

    text = weekly_report.build_message(
        current, previous, weekly_report.Conclusions(note=weekly_report.API_FAILED_NOTE)
    )
    assert text.endswith(weekly_report.API_FAILED_NOTE)
    assert len(text) <= report.MESSAGE_LIMIT


def test_model_sections_are_not_marked(filled):
    current, previous = filled
    text = weekly_report.build_message(
        current, previous,
        weekly_report.Conclusions("Вывод Нике.", "Вывод по вакансии.", "причина"),
    )
    assert "причина" not in text, "это слова модели, объясняться не нужно"
    assert "Вывод Нике." in text and "Вывод по вакансии." in text


def test_half_model_half_code_is_marked(filled):
    current, previous = filled
    text = weekly_report.build_message(
        current, previous,
        weekly_report.Conclusions("Вывод Нике.", "", weekly_report.EMPTY_ANSWER_NOTE),
    )
    assert "Вывод Нике." in text
    assert report.vacancy_fallback(current) in text
    assert text.endswith(weekly_report.EMPTY_ANSWER_NOTE)


def test_nika_fallback_names_the_most_important_thing_first():
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    posts = read_placements(placement_values(["2026-10-14", "Авито", "", ""]))
    touches = read_outreach(outreach_values(["2026-10-14", 20, "", "", ""]))

    # Учёт не заведён — это важнее всего остального.
    nothing = report.stats(week, [], [], [])
    assert "Учёт не заведён" in report.nika_fallback(nothing)

    # Учёт есть, но метку не ставят — отклики не на что записать.
    rows = [
        data_row(id=f"n{i}", created_at="2026-10-14 10:00:00", source="")
        for i in range(3)
    ]
    unlabelled = report.stats(
        week, report.read_responses(data_values(*rows)), posts, touches
    )
    assert "без метки 3" in report.nika_fallback(unlabelled)

    # Метки ставят, каналы пусты — замени каналы.
    empty = report.stats(week, [], posts, touches)
    assert "Авито" in report.nika_fallback(empty)


@pytest.mark.parametrize(
    "setup,expected",
    [
        ({}, "Откликов нет"),
        ({"responses": 5, "rejected": 3, "reasons": {"часы": 2, "техника": 1}},
         "чаще всего — часы (2)"),
        ({"responses": 5, "verdicts": {"B": 2, "C": 3}}, "Сильных кандидатов нет"),
        ({"responses": 5, "verdicts": {"A": 1}}, "к вакансии вопросов нет"),
    ],
    ids=["нет откликов", "отсев", "нет сильных", "всё хорошо"],
)
def test_vacancy_fallback_is_never_empty(setup, expected):
    week = report.Period(date(2026, 10, 12), date(2026, 10, 18))
    current = report.stats(week, [], [], [])
    for name, value in setup.items():
        setattr(current, name, value)

    text = report.vacancy_fallback(current)
    assert text, "об оффере всегда есть что сказать"
    assert expected in text


# --------------------------------- промпт ---------------------------------

def test_prompt_forbids_the_model_to_do_arithmetic():
    assert "Не считай ничего сам" in PROMPT
    assert "ни одного числа от себя" in PROMPT.lower()


def test_prompt_asks_for_exactly_two_sections():
    assert "**nika**" in PROMPT and "**vacancy**" in PROMPT
    assert '{"nika": "…", "vacancy": "…"}' in PROMPT
    assert "по две короткие фразы" in PROMPT


def test_prompt_forbids_markup_and_restating_numbers():
    assert "Заголовки разделов ставит код" in PROMPT
    assert "Не пересказывай цифры" in PROMPT
    assert "200 знаков" in PROMPT, "у разделов должен быть предел длины"


def test_prompt_knows_the_daily_plan():
    assert str(PLAN_PLACEMENTS_DAY) in PROMPT
    assert "личных касаний" in PROMPT


def test_prompt_covers_an_empty_week():
    assert "Откликов нет" in PROMPT
    assert "без метки" in PROMPT


def test_report_model_is_the_agreed_one():
    from app import config

    assert config.REPORT_MODEL == "claude-sonnet-4-6"


# ------------------------------- расписание -------------------------------

@pytest.fixture(scope="module")
def wf():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    data["_on"] = data.get("on") or data.get(True)
    return data


def test_schedule_is_monday_nine_moscow(wf):
    """09:00 МСК — это 06:00 UTC: GitHub считает расписание в UTC."""
    assert wf["_on"]["schedule"] == [{"cron": "0 6 * * 1"}]
    assert "workflow_dispatch" in wf["_on"], "ручной запуск нужен для проверки"


def test_workflow_runs_the_report(wf):
    steps = wf["jobs"]["send"]["steps"]
    runs = " ".join(str(s.get("run", "")) for s in steps)
    assert "pip install -r requirements.txt" in runs
    assert "python weekly_report.py" in runs


def test_workflow_passes_the_secrets_it_needs(wf):
    env = next(
        s["env"] for s in wf["jobs"]["send"]["steps"] if "weekly_report.py" in str(s.get("run", ""))
    )
    for name in (
        "ANTHROPIC_API_KEY", "GOOGLE_CREDENTIALS_JSON", "SHEET_ID",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS",
    ):
        assert name in env, name
        assert "secrets." in env[name], f"{name} должен приходить из секретов"


def test_two_reports_about_one_week_never_run_at_once(wf):
    assert wf["concurrency"]["group"] == "report"
    assert wf["concurrency"]["cancel-in-progress"] is False


def test_actions_are_pinned(wf):
    for step in wf["jobs"]["send"]["steps"]:
        action = step.get("uses")
        if action:
            assert "@" in action and not action.endswith("@main"), action


# --------------------------------- по дням --------------------------------

def test_every_day_of_the_week_is_in_the_report(week):
    current, _ = week
    assert [d.day for d in current.days] == WEEK.days


def test_day_rows_carry_work_and_its_yield(week):
    current, _ = week
    by_day = {d.day: d for d in current.days}

    monday = by_day[date(2026, 10, 5)]
    assert monday.placements == 1
    assert monday.outreach_sent == 20
    assert monday.responses == 1
    assert monday.idle is False


def test_days_without_work_are_named_separately(week):
    """День без откликов и день без работы — разные провалы, и путать их
    нельзя: во втором виноват не канал."""
    current, _ = week
    assert date(2026, 10, 10) in current.idle_days
    assert date(2026, 10, 7) not in current.idle_days

    text = report.numbers_text(current, report.stats(BEFORE, [], [], []))
    assert "Пн 05.10: размещений 1, касаний 20, откликов 1" in text
    assert "Дни без работы — ни размещений, ни касаний:" in text
