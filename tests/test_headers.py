"""Раскладка колонок: зоны не пересекаются и совпадают с якорями таблицы."""
from HEADERS import (
    COL_FORM_END,
    COL_FORM_START,
    COL_MANUAL_START,
    COL_RESULT_END,
    COL_RESULT_START,
    COL_SCORES_END,
    COL_SCORES_START,
    COL_TOTAL_SCORE,
    HEADERS,
    a1_range,
    col_letter,
    index_of,
)


def test_column_count():
    assert len(HEADERS) == 54
    assert col_letter(54) == "BB"
    assert len(set(HEADERS)) == 54, "имена колонок должны быть уникальны"


def test_score_anchors_match_sheet_formula():
    # Формула в таблице: =IF(A{row}="","",SUM(AI{row}:AM{row}))
    assert col_letter(COL_SCORES_START) == "AI"
    assert col_letter(COL_SCORES_END) == "AM"
    assert [HEADERS[i - 1] for i in range(COL_SCORES_START, COL_SCORES_END + 1)] == [
        "score_q1", "score_q2", "score_q3", "score_q4", "score_q5",
    ]
    assert col_letter(COL_TOTAL_SCORE) == "AN"
    assert HEADERS[COL_TOTAL_SCORE - 1] == "total_score"


def test_write_zones_do_not_touch_total_score():
    """Ни одна зона записи не должна накрывать AN — там формула."""
    zones = [
        range(COL_FORM_START, COL_FORM_END + 1),
        range(COL_SCORES_START, COL_SCORES_END + 1),
        range(COL_RESULT_START, COL_RESULT_END + 1),
    ]
    for zone in zones:
        assert COL_TOTAL_SCORE not in zone, f"зона {zone} накрывает total_score"


def test_zones_are_contiguous_and_disjoint():
    assert COL_SCORES_START == COL_FORM_END + 1
    assert COL_RESULT_START == COL_TOTAL_SCORE + 1
    assert COL_MANUAL_START == COL_RESULT_END + 1


def test_manual_columns_are_last():
    assert HEADERS[COL_MANUAL_START - 1] == "manual_note"
    assert HEADERS[-1] == "outcome"


def test_a1_ranges():
    assert a1_range(7, COL_FORM_START, COL_FORM_END) == "A7:AH7"
    assert a1_range(7, COL_SCORES_START, COL_SCORES_END) == "AI7:AM7"
    assert a1_range(7, COL_RESULT_START, COL_RESULT_END) == "AO7:AZ7"


def test_status_and_flags_positions():
    assert col_letter(index_of("status")) == "AU"
    assert col_letter(index_of("flags")) == "AS"
    assert col_letter(index_of("telegram")) == "D"
