"""Проверки без обращения к модели, на фикстурах."""
from types import SimpleNamespace

import pytest

from app import checks
from app.models import Telemetry
from app.numbers import MonthNumbers, Q1Numbers, Q4Numbers, parse_q1, parse_q4


def _sub(fixture):
    return SimpleNamespace(telemetry=Telemetry.model_validate(fixture["telemetry"]))


def test_flags_match_fixture(fixture):
    """Телеметрические и числовые флаги вместе дают ожидаемый набор."""
    if fixture["expected"].get("early_exit"):
        pytest.skip("ранний выход по стоп-вопросам, флаги не считаются")

    flags = set(checks.telemetry_flags(_sub(fixture)))
    q1, _ = parse_q1(fixture["answers"]["q1_text"])
    q4, _ = parse_q4(fixture["answers"]["q4_text"])
    flags |= set(checks.numeric_flags(q1, q4))
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


def _q1(*rows) -> Q1Numbers:
    return Q1Numbers(
        months=[
            MonthNumbers(month=m, deals=d, revenue=r, check=c) for m, d, r, c in rows
        ]
    )


def _q4(touch=0, deal=0, cycle=0) -> Q4Numbers:
    return Q4Numbers(conv_touch=touch, conv_deal=deal, cycle_days=cycle)


def test_math_within_tolerance():
    """Допуск 10%: расхождение в 5% флага не даёт, в 20% — даёт."""
    ok = _q1(("2026-05", 4, 420000, 100000))
    assert checks.FLAG_MATH not in checks.numeric_flags(ok, _q4())

    bad = _q1(("2026-05", 4, 480000, 100000))
    assert checks.FLAG_MATH in checks.numeric_flags(bad, _q4())


def test_empty_months_give_no_flags():
    """Нули — не расхождение: месяц просто пустой, за это отвечает балл."""
    assert checks.numeric_flags(_q1(("2026-05", 0, 0, 0)), _q4()) == []
    assert checks.numeric_flags(Q1Numbers(), _q4()) == []


def test_conversion_outlier():
    assert checks.FLAG_OUTLIER in checks.numeric_flags(Q1Numbers(), _q4(touch=51))
    assert checks.FLAG_OUTLIER not in checks.numeric_flags(Q1Numbers(), _q4(touch=50))
    assert checks.FLAG_OUTLIER in checks.numeric_flags(Q1Numbers(), _q4(deal=80))


# ------------------------------ балл Q1 -----------------------------------

STABLE = (("2026-03", 4, 480000, 120000),
          ("2026-04", 3, 360000, 120000),
          ("2026-05", 5, 600000, 120000))


def test_q1_three_stable_months_give_three():
    assert checks.q1_score(_q1(*STABLE), definition_ok=True) == 3


def test_q1_without_definition_caps_at_two():
    """Цифры без внятного определения сделки выше двойки не поднимаются."""
    assert checks.q1_score(_q1(*STABLE), definition_ok=False) == 2


def test_q1_all_zeroes_give_zero():
    zeroes = _q1(("2026-03", 0, 0, 0), ("2026-04", 0, 0, 0), ("2026-05", 0, 0, 0))
    assert checks.q1_score(zeroes, definition_ok=True) == 0


@pytest.mark.parametrize("empty_months", [1, 2])
def test_q1_incomplete_months_give_one(empty_months):
    """Нули хотя бы в одном месяце — данных за три месяца нет."""
    rows = list(STABLE)
    for i in range(empty_months):
        month = rows[i][0]
        rows[i] = (month, 0, 0, 0)
    assert checks.q1_score(_q1(*rows), definition_ok=True) == 1


def test_q1_few_deals_give_two():
    """Три месяца есть, но поток меньше трёх сделок — это ещё не стабильность."""
    rows = [("2026-03", 2, 240000, 120000),
            ("2026-04", 3, 360000, 120000),
            ("2026-05", 4, 480000, 120000)]
    assert checks.q1_score(_q1(*rows), definition_ok=True) == 2


def test_q1_check_spread_gives_two():
    """Чек, разошедшийся больше чем вдвое, стабильным результатом не считается."""
    ok = [("2026-03", 4, 240000, 60000),
          ("2026-04", 4, 480000, 120000),
          ("2026-05", 4, 400000, 100000)]
    assert checks.q1_score(_q1(*ok), definition_ok=True) == 3, "ровно 2× — ещё сопоставимо"

    wide = [("2026-03", 4, 240000, 60000),
            ("2026-04", 4, 484000, 121000),
            ("2026-05", 4, 400000, 100000)]
    assert checks.q1_score(_q1(*wide), definition_ok=True) == 2


def test_q1_score_survives_round_trip():
    """Балл считается по ячейке таблицы, а не по объекту в памяти."""
    from app.numbers import format_q1

    line = format_q1(_q1(*STABLE), "Закрытой считал сделку по дате акта.")
    restored, text = parse_q1(line)
    assert checks.q1_score(restored, definition_ok=True) == 3
    assert text == "Закрытой считал сделку по дате акта."
