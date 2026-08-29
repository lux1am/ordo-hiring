"""Проверки без обращения к модели, на фикстурах."""
from types import SimpleNamespace

import pytest

from app import checks
from app.models import Telemetry
from tests.conftest import CLEAN_PROFILE, PASSING_STOPS, SHEET_STOPS


def _sub(fixture):
    return SimpleNamespace(telemetry=Telemetry.model_validate(fixture["telemetry"]))


def test_flags_match_fixture(fixture):
    """Флаги телеметрии и вопросов о мотивации вместе дают ожидаемый набор."""
    if fixture["expected"].get("early_exit"):
        pytest.skip("ранний выход по стоп-вопросам, флаги не считаются")

    flags = set(checks.telemetry_flags(_sub(fixture)))
    flags |= set(checks.profile_flags(fixture["profile"]))
    assert flags == set(fixture["expected"]["flags"]), fixture["name"]


def test_mobile_disables_suspicious():
    """Нулевые бэкспейсы на телефоне — норма, а не подозрение."""
    tele = {
        "is_mobile": True,
        "fields": {"q1_text": {"length": 900, "pasted_chars": 0, "speed_cpm": 650, "backspaces": 0}},
    }
    flags = checks.telemetry_flags(_sub({"telemetry": tele}))
    assert checks.FLAG_SUSPICIOUS not in flags
    assert checks.FLAG_PASTED not in flags, "650 зн/мин на телефоне ниже порога 700"
    assert checks.FLAG_MOBILE in flags

    tele["is_mobile"] = False
    desktop = checks.telemetry_flags(_sub({"telemetry": tele}))
    assert checks.FLAG_SUSPICIOUS in desktop
    assert checks.FLAG_PASTED in desktop, "650 зн/мин на компьютере выше порога 400"


def test_paste_threshold_is_exclusive():
    """Ровно 30% — ещё не флаг, 31% — уже флаг."""
    def flags_for(pasted):
        tele = {"fields": {"q1_text": {"length": 100, "pasted_chars": pasted, "speed_cpm": 100}}}
        return checks.telemetry_flags(_sub({"telemetry": tele}))

    assert checks.FLAG_PASTED not in flags_for(30)
    assert checks.FLAG_PASTED in flags_for(31)


def test_tab_switch_only_when_return_precedes_answer():
    """Считается уход, после которого кандидат дописывал ответ."""
    tele = {"fields": {"q1_text": {"length": 200, "away_before_input_sec": 61}}}
    assert checks.FLAG_TAB_SWITCH in checks.telemetry_flags(_sub({"telemetry": tele}))

    tele = {"fields": {"q1_text": {"length": 200, "away_before_input_sec": 59}}}
    assert checks.FLAG_TAB_SWITCH not in checks.telemetry_flags(_sub({"telemetry": tele}))


# --------------------------- стоп-вопросы ---------------------------------

def test_passing_set_has_no_failures():
    assert checks.stop_failures(PASSING_STOPS) == []
    assert checks.stop_failures(SHEET_STOPS) == [], "уточнение в ячейке не мешает сверке"


@pytest.mark.parametrize(
    "field,value",
    [
        ("stop_1_sdelno", "Сомневаюсь"),
        ("stop_1_sdelno", "Нет, нужен оклад"),
        ("stop_2_chasy", "10–14"),
        ("stop_3_svyaz", "Нет"),
        ("stop_4_oformlenie", "Не подходит ни один"),
        ("stop_5_tehnika", "Частично"),
        ("stop_5_tehnika", "Нет"),
        ("stop_6_zapis", "Сомневаюсь"),
        ("stop_6_zapis", "Нет"),
    ],
)
def test_failing_values_are_pinpointed(field, value):
    assert checks.stop_failures({**PASSING_STOPS, field: value}) == [field]


def test_empty_answer_does_not_pass():
    """Пустая ячейка — это не «вопрос не задавали», а «ответа нет»."""
    assert checks.stop_failures({**PASSING_STOPS, "stop_3_svyaz": ""}) == [
        "stop_3_svyaz"
    ]
    assert set(checks.stop_failures({})) == set(checks.STOP_PASS)


def test_timezone_is_stripped_before_comparison():
    stops = {**PASSING_STOPS, "stop_3_svyaz": "Нет — Ваш часовой пояс: МСК+5"}
    assert checks.stop_failures(stops) == ["stop_3_svyaz"]


def test_passing_set_covers_every_stop_question():
    """Забытый вопрос молча пропускал бы всех — набор проверяется целиком."""
    from app.profile import stop_questions

    assert set(checks.STOP_PASS) == {q["field"] for q in stop_questions()}


# ------------------------- вопросы-флаги ----------------------------------

def test_clean_profile_gives_no_flags():
    assert checks.profile_flags(CLEAN_PROFILE) == []


@pytest.mark.parametrize("motive", ["Уйти на удалёнку", "Сменить сферу"])
def test_single_avoidance_motive_flags(motive):
    profile = {**CLEAN_PROFILE, "flag_motive": [motive]}
    assert checks.FLAG_MOTIVATION_AVOIDANCE in checks.profile_flags(profile)


@pytest.mark.parametrize("motive", ["Уйти на удалёнку", "Сменить сферу"])
def test_avoidance_with_second_motive_does_not_flag(motive):
    """Бегство — это единственный выбор, а не один из двух."""
    profile = {**CLEAN_PROFILE, "flag_motive": [motive, "Высокий доход без потолка"]}
    assert checks.FLAG_MOTIVATION_AVOIDANCE not in checks.profile_flags(profile)


def test_both_avoidance_motives_together_do_not_flag():
    profile = {**CLEAN_PROFILE, "flag_motive": ["Уйти на удалёнку", "Сменить сферу"]}
    assert checks.FLAG_MOTIVATION_AVOIDANCE not in checks.profile_flags(profile)


@pytest.mark.parametrize(
    "income,flag",
    [
        ("до 50 000 ₽", checks.FLAG_INCOME_LOW),
        ("более 150 000 ₽", checks.FLAG_INCOME_UNREAL),
    ],
)
def test_income_edges_flag(income, flag):
    assert flag in checks.profile_flags({**CLEAN_PROFILE, "flag_income": income})


@pytest.mark.parametrize("income", ["50 000–80 000 ₽", "80 000–120 000 ₽", "120 000–150 000 ₽"])
def test_income_middle_is_silent(income):
    assert checks.profile_flags({**CLEAN_PROFILE, "flag_income": income}) == []


def test_stability_first():
    rank = {
        "flag_rank_stability": "1",
        "flag_rank_income": "2",
        "flag_rank_schedule": "3",
        "flag_rank_growth": "4",
    }
    flags = checks.profile_flags({**CLEAN_PROFILE, "flag_rank": rank})
    assert checks.FLAG_STABILITY_FIRST in flags
    assert checks.FLAG_INCOME_NOT_PRIORITY not in flags, "доход на втором месте — приоритет"


@pytest.mark.parametrize("place", ["3", "4"])
def test_income_not_priority(place):
    rank = {**CLEAN_PROFILE["flag_rank"], "flag_rank_income": place}
    assert checks.FLAG_INCOME_NOT_PRIORITY in checks.profile_flags(
        {**CLEAN_PROFILE, "flag_rank": rank}
    )


@pytest.mark.parametrize(
    "answer,flag",
    [
        ("Легко достижимо", checks.FLAG_UNDERESTIMATE),
        ("Нереально", checks.FLAG_PESSIMIST),
    ],
)
def test_meetings_edges_flag(answer, flag):
    assert flag in checks.profile_flags({**CLEAN_PROFILE, "flag_meetings": answer})


@pytest.mark.parametrize("answer", ["Достижимо при усилиях", "Сложно, но возможно"])
def test_meetings_middle_is_silent(answer):
    assert checks.profile_flags({**CLEAN_PROFILE, "flag_meetings": answer}) == []


def test_no_goals():
    assert checks.FLAG_NO_GOALS in checks.profile_flags(
        {**CLEAN_PROFILE, "flag_goals": "Не знаю"}
    )


def test_empty_profile_does_not_crash():
    """Строка из таблицы могла прийти без вопросов-флагов вовсе."""
    assert checks.profile_flags({}) == []
