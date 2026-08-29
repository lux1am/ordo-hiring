"""Валидация анкеты и защита формы."""
import pytest
from pydantic import ValidationError

from app.models import OPEN_FIELDS, STOP_FIELDS, Submission, Telemetry
from tests.conftest import CLEAN_PROFILE, SHEET_STOPS

# Длиннее самого строгого минимума — 80 знаков у вопросов 1 и 4.
GOOD_ANSWER = "Разберу, на каком шаге срываются, и поменяю первую фразу. " * 2
# Третий вопрос жёстко ограничен семьюдесятью знаками.
SHORT_ANSWER = "Иван, за 15 минут покажу, как снизить цену заявки. Когда удобно?"


def answers(**over) -> dict:
    data = {f: GOOD_ANSWER for f in OPEN_FIELDS}
    data["q3_text"] = SHORT_ANSWER
    data.update(over)
    return data


def base(**over):
    data = dict(
        fio="Иванов Иван Иванович",
        telegram="@ivanov",
        resume_url="https://hh.ru/resume/123",
        pdn_consent=True,
        stops=dict(SHEET_STOPS),
        profile=dict(CLEAN_PROFILE),
        answers=answers(),
        telemetry=Telemetry(),
    )
    data.update(over)
    return data


def test_valid():
    s = Submission(**base())
    assert s.telegram == "@ivanov"
    assert s.answers["q3_text"] == SHORT_ANSWER


def test_answers_are_normalized():
    """Длина меряется по схлопнутым пробелам — так же, как в счётчике."""
    s = Submission(**base(answers=answers(q1_text="  а  б  " + GOOD_ANSWER)))
    assert "  " not in s.answers["q1_text"]


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
        Submission(**base(answers=answers(q1_text="мало")))


def test_q3_length_limit():
    """Семьдесят знаков — жёсткий предел, а не пожелание."""
    with pytest.raises(ValidationError, match="70"):
        Submission(**base(answers=answers(q3_text="я" * 71)))


def test_q3_has_no_minimum():
    """У короткого задания нижнего порога нет — важна только уместность."""
    s = Submission(**base(answers=answers(q3_text="Иван, 15 минут в четверг?")))
    assert s.answers["q3_text"] == "Иван, 15 минут в четверг?"


def test_empty_answer_rejected():
    with pytest.raises(ValidationError, match="Не заполнен"):
        Submission(**base(answers=answers(q3_text="   ")))


def test_all_stop_questions_required():
    with pytest.raises(ValidationError, match="условиях работы"):
        Submission(**base(stops={STOP_FIELDS[0]: "Да, полностью готов"}))


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
