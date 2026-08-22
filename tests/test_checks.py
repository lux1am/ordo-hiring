"""Проверки без обращения к модели, на фикстурах."""
from types import SimpleNamespace

import pytest

from app import checks
from app.models import ExtractorOutput, Telemetry


def _sub(fixture):
    return SimpleNamespace(telemetry=Telemetry.model_validate(fixture["telemetry"]))


def test_flags_match_fixture(fixture):
    """Телеметрические и числовые флаги вместе дают ожидаемый набор."""
    if fixture["expected"].get("early_exit"):
        pytest.skip("ранний выход по стоп-вопросам, флаги не считаются")

    flags = set(checks.telemetry_flags(_sub(fixture)))
    extracted = (
        ExtractorOutput.model_validate(fixture["extracted"])
        if fixture.get("extracted")
        else None
    )
    flags |= set(checks.numeric_flags(extracted))
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


def test_math_within_tolerance():
    """Допуск 10%: расхождение в 5% флага не даёт, в 20% — даёт."""
    ok = ExtractorOutput.model_validate(
        {"months": [{"month": "май", "deals": 4, "revenue": 420000, "avg_check": 100000}]}
    )
    assert checks.FLAG_MATH not in checks.numeric_flags(ok)

    bad = ExtractorOutput.model_validate(
        {"months": [{"month": "май", "deals": 4, "revenue": 480000, "avg_check": 100000}]}
    )
    assert checks.FLAG_MATH in checks.numeric_flags(bad)


def test_no_numbers_instead_of_math():
    """Экстрактор не отработал — NO_NUMBERS, а не MATH."""
    assert checks.numeric_flags(None) == [checks.FLAG_NO_NUMBERS]
    empty = ExtractorOutput.model_validate({"months": [], "conv_touch_meeting": None})
    assert checks.numeric_flags(empty) == [checks.FLAG_NO_NUMBERS]
    assert checks.FLAG_MATH not in checks.numeric_flags(empty)


def test_conversion_outlier():
    over = ExtractorOutput.model_validate({"months": [], "conv_touch_meeting": 51})
    assert checks.FLAG_OUTLIER in checks.numeric_flags(over)
    under = ExtractorOutput.model_validate({"months": [], "conv_touch_meeting": 50})
    assert checks.FLAG_OUTLIER not in checks.numeric_flags(under)


def test_q1_manual_needs_two_full_months():
    """Месяц засчитан только при всех трёх числах сразу."""
    partial = ExtractorOutput.model_validate(
        {"months": [
            {"month": "март", "deals": 4, "revenue": 400000, "avg_check": 100000},
            {"month": "апрель", "deals": 3, "revenue": 300000, "avg_check": None},
        ]}
    )
    assert checks.q1_needs_manual(partial), "второй месяц без чека не считается подтверждённым"

    full = ExtractorOutput.model_validate(
        {"months": [
            {"month": "март", "deals": 4, "revenue": 400000, "avg_check": 100000},
            {"month": "апрель", "deals": 3, "revenue": 300000, "avg_check": 100000},
        ]}
    )
    assert not checks.q1_needs_manual(full)
    assert checks.q1_needs_manual(None)
