"""Карточка кандидата: формулы, из которых она собрана.

Формула, промахнувшаяся мимо колонки, не падает — она молча показывает
пустоту или чужой ответ, и заметить это на созвоне с кандидатом поздно.
Поэтому здесь проверяется адрес каждой ячейки: та ли колонка «Данных», тот
ли проходной набор, то ли название флага.

Работа с книгой проверяется в test_migration.py — там поднят лист в памяти.
"""
import re

import pytest

import sync_questions as sq
from HEADERS import col_letter, index_of
from app import checks, ru, verdict

# Флаги о поведении в форме ставятся по телеметрии, а не по ответу на
# вопрос: в блоке мотивации им делать нечего.
TELEMETRY = set(verdict.TELEMETRY_FLAGS) | {checks.FLAG_MOBILE}

PROFILE_FLAGS = {
    value for name, value in vars(checks).items() if name.startswith("FLAG_")
} - TELEMETRY


@pytest.fixture(scope="module")
def card():
    values, _ = sq.card_values()
    return values


@pytest.fixture(scope="module")
def bold():
    _, rows = sq.card_values()
    return rows


def block(card: list[list[str]], title: str) -> list[list[str]]:
    """Строки блока: от заголовка до пустой строки или конца листа."""
    start = next(i for i, row in enumerate(card) if row[0] == title)
    out = []
    for row in card[start + 1:]:
        if not any(row):
            break
        out.append(row)
    return out


def column(name: str) -> str:
    """«'Данные'!AX:AX» — так колонка выглядит внутри формулы."""
    letter = col_letter(index_of(name))
    return f"'Данные'!{letter}:{letter}"


def balanced(formula: str) -> bool:
    """Скобки закрыты, кавычки закрыты. Грубая проверка синтаксиса формулы.

    Ловит ровно то, чем опасна сборка формул строками: лишнюю скобку и
    незакрытый строковый литерал. Внутри литерала кавычка удваивается.
    """
    depth, inside, i = 0, False, 1
    while i < len(formula):
        char = formula[i]
        if inside:
            if char == '"':
                if formula[i + 1:i + 2] == '"':
                    i += 1
                else:
                    inside = False
        elif char == '"':
            inside = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and not inside


# ------------------------------ каркас ------------------------------------

def test_card_is_three_columns_wide(card):
    assert all(len(row) == sq.CARD_COLS for row in card)


def test_blocks_go_in_order(card):
    titles = [row[0] for row in card if row[0] and not any(row[1:])]
    assert titles == [
        sq.STOPS_TITLE, sq.FLAGS_TITLE, sq.ANSWERS_TITLE, sq.SCREENING_TITLE
    ]


def test_only_block_titles_are_bold(card, bold):
    assert [card[i - 1][0] for i in bold] == [
        sq.STOPS_TITLE, sq.FLAGS_TITLE, sq.ANSWERS_TITLE, sq.SCREENING_TITLE
    ]


def test_every_formula_is_syntactically_whole(card):
    formulas = [c for row in card for c in row if c.startswith("=")]
    assert len(formulas) > 20, "карточка держится на формулах"
    for formula in formulas:
        assert balanced(formula), formula


def test_all_formulas_read_the_selected_name(card):
    """Прямо из A1 или через ячейку с ответом, которая сама смотрит туда."""
    for row in card:
        for value in row[1:]:
            if value.startswith("="):
                assert sq.NAME_CELL in value or re.search(r"\$B\d+", value), value


def test_card_does_not_depend_on_the_run():
    assert sq.card_values() == sq.card_values()


# ------------------------------ шапка -------------------------------------

def test_top_keeps_the_name_cell_empty(card):
    assert card[0][0] == "", "в A1 ложится выбранная фамилия, а не текст"


def test_top_shows_decision_level_score_and_telegram(card):
    top = "\n".join("\t".join(row) for row in card[:2])
    assert column("decision_ru") in top, "решение"
    assert column("verdict") in top, "уровень"
    assert column("total_score") in top, "баллы"
    assert column("telegram") in top, "телеграм"
    assert f'" из {sq.MAX_TOTAL}"' in top


# ------------------------------ УСЛОВИЯ -----------------------------------

def test_stop_block_has_every_stop_question(card):
    rows = block(card, sq.STOPS_TITLE)
    questions = sq.stop_questions()
    assert len(rows) == len(questions) == 6

    for row, question in zip(rows, questions):
        assert row[0] == question["text"]
        assert column(question["field"]) in row[1]


def test_stop_marks_check_the_passing_options(card):
    rows = block(card, sq.STOPS_TITLE)
    for row, question in zip(rows, sq.stop_questions()):
        allowed = checks.STOP_PASS[question["field"]]
        for option in question["options"]:
            wanted = f'EXACT(o,"{option}")' in row[2]
            assert wanted == (option in allowed), f"{question['field']}: {option}"
        assert sq.PASS_MARK in row[2] and sq.FAIL_MARK in row[2]


def test_stop_mark_looks_at_the_answer_cell_of_its_own_row(card):
    rows = block(card, sq.STOPS_TITLE)
    first = next(i for i, row in enumerate(card, start=1) if row[0] == sq.STOPS_TITLE) + 1
    for offset, row in enumerate(rows):
        assert f"LET(a,$B{first + offset}," in row[2]


def test_stop_mark_cuts_off_the_appended_detail(card):
    """К «Да» приписан часовой пояс — сравнивать нужно ответ, а не ячейку."""
    rows = block(card, sq.STOPS_TITLE)
    tz = next(r for r, q in zip(rows, sq.stop_questions()) if q.get("extra"))
    assert f'SPLIT(a,"{sq.STOP_SEP.strip()}")' in tz[2]


# --------------------- МОТИВАЦИЯ И ОЖИДАНИЯ -------------------------------

def test_flag_block_has_every_flag_question(card):
    rows = block(card, sq.FLAGS_TITLE)
    questions = sq.flag_questions()
    assert len(rows) == len(questions) == 5

    for row, question in zip(rows, questions):
        assert row[0] == question["text"]
        assert column("ai_markers") in row[1], "ответы едут в ai_markers"


def test_flag_answers_dig_out_their_own_key(card):
    rows = block(card, sq.FLAGS_TITLE)
    for row, question in zip(rows, sq.flag_questions()):
        if question["type"] == "rank":
            for line in question["rows"]:
                assert line["field"] in row[1]
            assert all(
                (line.get("short") or line["label"]) in row[1]
                for line in question["rows"]
            )
        else:
            assert question["field"] in row[1]


def test_flag_marks_name_the_flag_in_russian(card):
    rows = block(card, sq.FLAGS_TITLE)
    named = set()
    for row, question in zip(rows, sq.flag_questions()):
        assert column("flags") in row[2], "флаг ставит обработчик, карточка его читает"
        for _, flag in sq.flag_rules()[question["field"]]:
            assert f'"\\b{flag}\\b"' in row[2]
            assert ru.FLAG_NAMES[flag] in row[2]
            named.add(flag)
    assert named == PROFILE_FLAGS, "флаг без расшифровки в карточке не виден"


def test_flag_rules_are_written_for_every_flag_question():
    assert set(sq.flag_rules()) == {q["field"] for q in sq.flag_questions()}


# ------------------------------ ОТВЕТЫ ------------------------------------

def test_answers_block_carries_the_whole_question(card):
    rows = block(card, sq.ANSWERS_TITLE)
    questions = sq.open_questions()
    assert len(rows) == len(questions) == 5

    for row, question in zip(rows, questions):
        assert row[0] == question["text"], "формулировка целиком, а не сокращённая"
        assert column(question["field"]) in row[1]


def test_answers_show_the_score_out_of_three(card):
    rows = block(card, sq.ANSWERS_TITLE)
    for row, question in zip(rows, sq.open_questions()):
        assert column(sq.score_column(question["field"])) in row[2]
        assert f'" из {sq.MAX_PER_QUESTION}"' in row[2]


# ------------------- ВОПРОСЫ К СКРИНИНГУ ----------------------------------

def test_screening_splits_the_column_into_rows(card):
    rows = block(card, sq.SCREENING_TITLE)
    assert len(rows) == sq.SCREENING_ROWS + 1, "все вопросы и «Что смущает»"

    first = next(
        i for i, row in enumerate(card, start=1) if row[0] == sq.SCREENING_TITLE
    ) + 1
    for number, row in enumerate(rows[:sq.SCREENING_ROWS], start=1):
        assert f'"Вопрос {number}"' in row[0]
        assert column("screening_questions") in row[1]
        assert f"CHAR(10)),1,{number})" in row[1], "вопросы разделены переводом строки"
        assert f'IF($B{first + number - 1}=""' in row[0], (
            "подпись показывается только у непустой строки"
        )


def test_screening_block_fits_everything_the_pipeline_writes():
    """Модель даёт ровно три вопроса, телеметрия дописывает четвёртый.

    Строк должно хватать на худший случай: недостающая строка молча
    проглотила бы вопрос, до которого на созвоне уже не дойдут.
    """
    extra = verdict.screening_additions(list(verdict.TELEMETRY_FLAGS))
    assert extra, "телеметрия обязана давать вопрос голосом"
    assert sq.SCREENING_ROWS == sq.MODEL_SCREENING_ROWS + len(extra) == 4


def test_concerns_row_comes_from_flags_ru(card):
    concerns = block(card, sq.SCREENING_TITLE)[-1]
    assert concerns[0] == sq.CONCERNS_LABEL
    assert column("flags_ru") in concerns[1]


# ------------------------- оформление и печать ----------------------------

def test_column_widths_follow_the_spec():
    assert sq.COLUMN_CHARS == (55, 60, 14)
    assert sq.column_widths() == [385, 420, 98]


def test_name_source_is_the_fio_column_without_the_header():
    fio = col_letter(index_of("fio"))
    assert sq.name_source() == f"='Данные'!${fio}$2:${fio}"


def test_export_url_is_a_portrait_one_pager():
    url = sq.export_url(42, 30)
    assert "gid=42" in url and "portrait=true" in url
    assert "scale=4" in url, "вписать в страницу"
    assert f"range=A1:{col_letter(sq.CARD_COLS)}30" in url


def test_sheet_visibility_lists_do_not_overlap():
    assert not set(sq.HIDDEN_SHEETS) & set(sq.VISIBLE_SHEETS)
    assert sq.CARD_TITLE in sq.VISIBLE_SHEETS
    assert sq.OLD_SHEET_TITLE not in sq.VISIBLE_SHEETS + sq.HIDDEN_SHEETS


# ------------------------- сборка строк формул ----------------------------

def test_literal_doubles_the_quotes():
    assert sq.lit('он сказал "нет"') == '"он сказал ""нет"""'


def test_score_column_only_for_open_questions():
    assert sq.score_column("q3_text") == "score_q3"
    assert sq.score_column("stop_1_sdelno") == ""
