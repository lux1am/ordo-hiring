"""Валидация анкеты и защита формы."""
import pytest
from pydantic import ValidationError

from app.models import OPEN_FIELDS, STOP_FIELDS, Submission, Telemetry

GOOD_ANSWER = "Март: 4 сделки, 480 000 ₽, средний чек 120 000 ₽. " * 3


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


def test_valid():
    s = Submission(**base())
    assert s.telegram == "@ivanov"
    assert not s.has_stop_no()


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
