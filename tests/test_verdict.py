"""Пороги, понижения и автофильтры."""
from app.verdict import TELEMETRY_SCREENING_RU, decide, screening_additions

FULL = {"q1": 3, "q2": 3, "q3": 3, "q4": 3, "q5": 3}


def test_verdict_matches_fixture(fixture):
    exp = fixture["expected"]
    if exp.get("scores") is None:
        return   # ранний выход по стоп-вопросам, скоринга не было

    v = decide(
        scores=exp["scores"],
        flags=exp["flags"],
        ai_style=fixture["ai_style"],
        q3_senior_dm_confirmed=fixture["auto_filters"]["q3_senior_dm_confirmed"],
    )
    assert v.total == exp["total"], fixture["name"]
    assert v.verdict == exp["verdict"], fixture["name"]


def test_thresholds():
    def verdict_for(total):
        scores = {"q1": 0, "q2": 0, "q3": 0, "q4": 0, "q5": 0}
        left = total
        for k in scores:
            scores[k] = min(3, left)
            left -= scores[k]
        return decide(scores, [], 0, True).verdict

    assert verdict_for(15) == "A"
    assert verdict_for(12) == "A"
    assert verdict_for(11) == "B"
    assert verdict_for(9) == "B"
    assert verdict_for(8) == "C"
    assert verdict_for(0) == "C"


def test_downgrade_a_to_b():
    for flags, style in ([["MATH"], 0], [[], 71]):
        assert decide(FULL, flags, style, True).verdict == "B", (flags, style)


def test_no_downgrade_below_a():
    """Понижение действует только на A: B при флагах остаётся B."""
    scores = {"q1": 2, "q2": 2, "q3": 2, "q4": 2, "q5": 2}   # 10 -> B
    assert decide(scores, ["PASTED", "MATH"], 90, True).verdict == "B"


def test_ai_style_boundary():
    assert decide(FULL, [], 70, True).verdict == "A", "ровно 70 — ещё не понижение"
    assert decide(FULL, [], 71, True).verdict == "B"


def test_other_flags_do_not_downgrade():
    """Понижают только MATH и высокий ai_style."""
    for flag in ("PASTED", "SUSPICIOUS", "TAB_SWITCH", "OUTLIER", "NO_NUMBERS", "MOBILE"):
        assert decide(FULL, [flag], 0, True).verdict == "A", flag


def test_telemetry_never_downgrades():
    """Как заполняли — не оценка. Даже все три флага сразу оставляют A."""
    v = decide(FULL, ["PASTED", "SUSPICIOUS", "TAB_SWITCH"], 0, True)
    assert v.verdict == "A"
    assert v.reasons == [], "понижения не было — писать в причины нечего"


def test_telemetry_adds_screening_question():
    """Вместо понижения — вопрос на интервью, по одному разу на анкету."""
    for flag in ("PASTED", "SUSPICIOUS", "TAB_SWITCH"):
        assert screening_additions([flag]) == [TELEMETRY_SCREENING_RU], flag

    assert screening_additions(["PASTED", "SUSPICIOUS", "TAB_SWITCH"]) == [
        TELEMETRY_SCREENING_RU
    ], "три флага не должны давать три одинаковых вопроса"


def test_non_telemetry_flags_add_nothing():
    for flag in ("MATH", "OUTLIER", "NO_NUMBERS", "MOBILE"):
        assert screening_additions([flag]) == [], flag
    assert screening_additions([]) == []


def test_q3_autofilter_overrides_everything():
    v = decide(FULL, [], 0, q3_senior_dm_confirmed=False)
    assert v.verdict == "C"
    assert v.total == 15, "сумма сохраняется, меняется только вердикт"
    assert any("автофильтру" in r for r in v.reasons)
