"""Еженедельный отчёт: цифры, текст, отправка, расписание.

Отчёт уходит раз в неделю, и проверить его «посмотрев в понедельник»
значит заметить ошибку через семь дней после того, как она появилась.
Поэтому недели здесь выдуманные, а цифры по ним считаются полностью: от
границ периода до конверсии личных касаний.

Главное, что эти тесты стерегут, — раздельный учёт источников. Каналы и
личные касания складывать нельзя: двадцать постов и двадцать сообщений в
личку это разная работа, и смысл отчёта в том, какая из них сработала.
"""
import json
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


# ------------------------------- сборка письма ----------------------------

def test_message_puts_code_numbers_above_model_text():
    message = weekly_report.build_message("ЦИФРЫ", "ВЫВОДЫ", "05.10–11.10")
    assert message.index("ЦИФРЫ") < message.index("ВЫВОДЫ")
    assert "Отчёт по подбору за неделю 05.10–11.10" in message


def test_message_goes_out_even_without_the_model():
    message = weekly_report.build_message("ЦИФРЫ", "", "05.10–11.10")
    assert "ЦИФРЫ" in message
    assert weekly_report.MODEL_FAILED_LINE in message


def test_long_report_is_split_by_paragraphs():
    text = "\n\n".join(f"абзац {i} " + "я" * 400 for i in range(20))
    parts = weekly_report.chunks(text)

    assert len(parts) > 1
    assert all(len(p) <= weekly_report.MESSAGE_LIMIT for p in parts)
    # Ни один абзац не должен порваться пополам.
    for i in range(20):
        assert any(f"абзац {i} " in p for p in parts)


def test_single_huge_paragraph_is_split_by_lines():
    text = "\n".join("строка " + "я" * 100 for _ in range(100))
    parts = weekly_report.chunks(text)
    assert len(parts) > 1
    assert all(len(p) <= weekly_report.MESSAGE_LIMIT for p in parts)


def test_short_report_stays_one_message():
    assert weekly_report.chunks("коротко") == ["коротко"]


# -------------------------------- отправка --------------------------------

class FakeTelegram:
    """Bot API в памяти: кому ушло, что ушло и кто отказал."""

    def __init__(self, failing: set[str] = frozenset()):
        self.failing = failing
        self.sent: list[tuple[str, str]] = []

    def post(self, method: str, payload: dict) -> dict:
        assert method == "sendMessage"
        chat = payload["chat_id"]
        if chat in self.failing:
            return {"ok": False, "description": "chat not found"}
        self.sent.append((chat, payload["text"]))
        return {"ok": True}


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(weekly_report, "telegram_chat_ids", lambda: ["111", "-100222"])
    return fake


def test_report_goes_to_every_recipient(telegram):
    sent, failed = weekly_report.broadcast("отчёт")
    assert (sent, failed) == (2, 0)
    assert [chat for chat, _ in telegram.sent] == ["111", "-100222"]


def test_one_bad_recipient_does_not_stop_the_others(monkeypatch):
    fake = FakeTelegram(failing={"111"})
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(weekly_report, "telegram_chat_ids", lambda: ["111", "222", "333"])

    sent, failed = weekly_report.broadcast("отчёт")
    assert (sent, failed) == (2, 1)
    assert [chat for chat, _ in fake.sent] == ["222", "333"]


def test_network_failure_of_one_recipient_is_isolated(monkeypatch):
    calls = []

    def post(method, payload):
        calls.append(payload["chat_id"])
        if payload["chat_id"] == "111":
            raise OSError("обрыв связи")
        return {"ok": True}

    monkeypatch.setattr(weekly_report, "post", post)
    monkeypatch.setattr(weekly_report, "telegram_chat_ids", lambda: ["111", "222"])

    assert weekly_report.broadcast("отчёт") == (1, 1)
    assert calls == ["111", "222"]


def test_message_is_sent_as_plain_text(telegram):
    """Разметка Телеграма споткнулась бы на звёздочках и подчёркиваниях в
    метках вроде lk_05_10 и вернула ошибку вместо сообщения."""
    weekly_report.broadcast("метка lk_05_10 и *звёздочка*")
    assert telegram.sent, "сообщение должно уйти как есть"
    assert telegram.sent[0][1] == "метка lk_05_10 и *звёздочка*"


def test_recipients_are_parsed_from_one_variable(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS_RAW", " 111, -100222 ,, ")
    assert config.telegram_chat_ids() == ["111", "-100222"]


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
    assert report.NO_RESPONSES_LINE in printed
    assert "Отчёт по подбору за неделю" in printed


def test_run_without_recipients_stops_before_counting(monkeypatch):
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: [])
    monkeypatch.setattr(weekly_report, "telegram_chat_ids", lambda: [])
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
    monkeypatch.setattr(weekly_report, "telegram_chat_ids", lambda: ["111"])
    monkeypatch.setattr(weekly_report, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(weekly_report, "conclusions", lambda numbers: "ВЫВОДЫ")

    assert weekly_report.main([]) == 0
    chat, text = fake.sent[0]
    assert chat == "111"
    assert "ВЫВОДЫ" in text and report.NO_RESPONSES_LINE in text


def test_failed_model_does_not_cancel_the_report(collected, monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: [])
    monkeypatch.setattr(weekly_report, "telegram_chat_ids", lambda: ["111"])
    monkeypatch.setattr(weekly_report, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(
        weekly_report, "client",
        lambda: (_ for _ in ()).throw(RuntimeError("модель недоступна")),
    )

    assert weekly_report.main([]) == 0
    assert report.NO_RESPONSES_LINE in fake.sent[0][1]


def test_everyone_failing_is_an_error(collected, monkeypatch):
    fake = FakeTelegram(failing={"111"})
    monkeypatch.setattr(weekly_report, "post", fake.post)
    monkeypatch.setattr(weekly_report, "missing_secrets", lambda: [])
    monkeypatch.setattr(weekly_report, "telegram_chat_ids", lambda: ["111"])
    monkeypatch.setattr(weekly_report, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(weekly_report, "conclusions", lambda numbers: "")

    assert weekly_report.main([]) == 1


# --------------------------------- промпт ---------------------------------

def test_prompt_forbids_the_model_to_do_arithmetic():
    assert "Не считай ничего сам" in PROMPT
    assert "ни одного числа от себя" in PROMPT.lower()


def test_prompt_asks_for_the_agreed_sections():
    for section in ("Каналы", "Провалы по дням", "Вопросы Нике", "Не к Нике"):
        assert section in PROMPT, section


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
