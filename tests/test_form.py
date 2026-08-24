"""Валидация анкеты и защита формы."""
import pytest
from pydantic import ValidationError

from app.models import OPEN_FIELDS, STOP_FIELDS, Submission, Telemetry
from app.numbers import MonthNumbers, Q1Numbers, Q4Numbers

# Длиннее самого строгого минимума — 150 знаков у вопроса 3.
GOOD_ANSWER = "Закрытой считал сделку по дате акта и полной оплате. " * 4


def base(**over):
    data = dict(
        fio="Иванов Иван Иванович",
        telegram="@ivanov",
        resume_url="https://hh.ru/resume/123",
        pdn_consent=True,
        stops={f: "Да" for f in STOP_FIELDS},
        answers={f: GOOD_ANSWER for f in OPEN_FIELDS},
        telemetry=Telemetry(),
    )
    data.update(over)
    return data


def numbers() -> dict:
    return dict(
        q1_numbers=Q1Numbers(months=[
            MonthNumbers(month="2026-03", deals=4, revenue=480000, check=120000),
            MonthNumbers(month="2026-04", deals=4, revenue=480000, check=120000),
            MonthNumbers(month="2026-05", deals=4, revenue=480000, check=120000),
        ]),
        q4_numbers=Q4Numbers(conv_touch=11, conv_deal=30, cycle_days=40),
    )


def test_valid():
    s = Submission(**base())
    assert s.telegram == "@ivanov"
    assert not s.has_stop_no()


def test_numbers_are_prepended_to_text():
    """Числа приписываются к ответу, а колонки таблицы остаются прежними."""
    s = Submission(**base(**numbers()))
    assert s.answers["q1_text"].startswith("Март 2026: 4 / 480000 / 120000; ")
    assert s.answers["q1_text"].endswith(GOOD_ANSWER.strip())
    assert s.answers["q4_text"].startswith(
        "Касание→встреча: 11%; Встреча→оплата: 30%; Цикл сделки: 40 дн — "
    )


def test_length_is_measured_before_numbers_are_added():
    """Минимум набирается текстом кандидата, а не цифрами из соседних полей.

    Иначе короткая отписка проходила бы проверку за счёт приписанных к ней
    чисел — строка с ними длиннее сорока знаков сама по себе.
    """
    short = {**{f: GOOD_ANSWER for f in OPEN_FIELDS}, "q1_text": "по акту"}
    with pytest.raises(ValidationError, match="слишком короткий"):
        Submission(**base(answers=short, **numbers()))


def test_answer_without_numbers_stays_untouched():
    """Без переданных чисел ответ ложится в ячейку как есть.

    Нули вместо непереданных полей означали бы, что кандидату молча
    записали результат, которого он не заявлял.
    """
    s = Submission(**base())
    assert s.answers["q1_text"] == GOOD_ANSWER.strip()
    assert s.answers["q4_text"] == GOOD_ANSWER.strip()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@ivanov", "@ivanov"),
        ("ivanov", "@ivanov"),
        ("t.me/ivanov", "@ivanov"),
        ("https://t.me/ivanov", "@ivanov"),
    ],
)
def test_telegram_normalized(raw, expected):
    assert Submission(**base(telegram=raw)).telegram == expected


@pytest.mark.parametrize("bad", ["", "@a", "иванов", "@ivan ov", "@" + "a" * 40])
def test_telegram_rejected(bad):
    with pytest.raises(ValidationError):
        Submission(**base(telegram=bad))


def test_consent_required():
    with pytest.raises(ValidationError, match="согласия"):
        Submission(**base(pdn_consent=False))


def test_short_answer_rejected():
    with pytest.raises(ValidationError, match="слишком короткий"):
        Submission(**base(answers={**{f: GOOD_ANSWER for f in OPEN_FIELDS}, "q1_text": "мало"}))


def test_q5_length_limit():
    with pytest.raises(ValidationError, match="700"):
        Submission(**base(answers={**{f: GOOD_ANSWER for f in OPEN_FIELDS}, "q5_text": "я" * 701}))


def test_all_stop_questions_required():
    with pytest.raises(ValidationError, match="условиях работы"):
        Submission(**base(stops={STOP_FIELDS[0]: "Да"}))


def test_stop_no_detected():
    s = Submission(**base(stops={**{f: "Да" for f in STOP_FIELDS}, "stop_1_oklad": "Нет"}))
    assert s.has_stop_no()
    assert s.rejected_conditions() == ["stop_1_oklad"]


def test_bad_url():
    with pytest.raises(ValidationError, match="http"):
        Submission(**base(resume_url="hh.ru/resume/123"))


def test_telemetry_is_clamped():
    """Телеметрия приходит от кандидата — ей нельзя доверять как есть."""
    t = Telemetry.model_validate({
        "total_time_sec": -5,
        "tab_switches": -1,
        "fields": {"q1_text": {"length": 10, "pasted_chars": 999, "speed_cpm": -3}},
    })
    assert t.total_time_sec == 0
    assert t.tab_switches == 0
    assert t.field("q1_text").pasted_chars == 10, "вставка не может превышать длину"
    assert t.field("q1_text").paste_pct == 100.0
    assert t.field("q1_text").speed_cpm == 0


def test_missing_telemetry_field_is_safe():
    assert Telemetry().field("q9_text").length == 0
