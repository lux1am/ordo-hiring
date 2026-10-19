"""Учёт размещений: формулы, из которых собраны четыре листа.

Формула в таблице не падает — она показывает пустоту, ноль или чужое
число. Заметить это по сводке, на которую смотрят раз в неделю, невозможно:
ноль откликов из канала выглядит как ноль откликов из канала. Поэтому здесь
проверяется адрес каждой ячейки, имя каждой функции и то, что метка в
ссылке — ровно та, которую потом ищет COUNTIF.

Работа с книгой проверяется в test_migration.py — там поднят лист в памяти.
"""
import re

import pytest

import placements as pl
from HEADERS import FIRST_DATA_ROW, col_letter, index_of
from app import ru, source
from app.config import (
    CHANNELS_SHEET,
    FORM_DOMAIN,
    OUTREACH_SHEET,
    PLACEMENTS_SHEET,
)

PLACEMENT_LAST = 1 + pl.PLACEMENT_ROWS
OUTREACH_LAST = 1 + pl.OUTREACH_ROWS


# --------------------------- проверка синтаксиса --------------------------
#
# Формула собирается склейкой строк, и ошибиться в ней можно ровно двумя
# способами: написать несуществующую функцию и сослаться на имя, которого в
# LET нет. Оба проверяются здесь, а не глазами в таблице.

# Функции Google Sheets, которыми собраны листы. Список закрытый: опечатка
# вроде SORTT или DATEDIFF, которой в Sheets нет, завалит тест.
KNOWN = {
    "AND", "CHOOSECOLS", "COUNTIF", "COUNTIFS", "FILTER", "HSTACK", "IF",
    "IFERROR", "IFNA", "INDEX", "ISNUMBER", "LAMBDA", "LEFT", "LET", "MAP",
    "OR", "ROUND",
    "ROWS", "SEQUENCE", "SORT", "SUM", "SUMIF", "SUMPRODUCT", "TEXT",
    "UNIQUE", "VSTACK",
}

LITERAL_RE = re.compile(r'"(?:[^"]|"")*"')
SHEET_REF_RE = re.compile(r"'[^']*'!(?:\$?[A-Z]+\$?\d*)(?::\$?[A-Z]+\$?\d*)?")
CELL_REF_RE = re.compile(r"\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d*)?")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def bare(formula: str) -> str:
    """Формула без литералов и адресов — остаются функции и имена."""
    out = LITERAL_RE.sub(" ", formula)
    out = SHEET_REF_RE.sub(" ", out)
    return CELL_REF_RE.sub(" ", out)


def functions(formula: str) -> set[str]:
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\(", bare(formula)))


def split_args(text: str) -> list[str]:
    """Аргументы вызова, разрезанные по запятым верхнего уровня."""
    args, depth, inside, start = [], 0, False, 0
    for i, char in enumerate(text):
        if inside:
            inside = char != '"'
        elif char == '"':
            inside = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            args.append(text[start:i])
            start = i + 1
    args.append(text[start:])
    return args


def call_body(text: str, start: int) -> str:
    """Содержимое скобок вызова, начинающегося на позиции start."""
    depth, inside = 0, False
    for i in range(start, len(text)):
        char = text[i]
        if inside:
            inside = char != '"'
        elif char == '"':
            inside = True
        elif char == "(":
            depth += 1
            if depth == 1:
                open_at = i
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i]
    raise AssertionError("не закрыта скобка вызова")


def let_names(formula: str) -> set[str]:
    """Все имена, объявленные во всех LET формулы, включая вложенные.

    LET принимает пары «имя, значение» и выражение последним аргументом,
    то есть аргументов всегда нечётное число. Разъехавшаяся пара — это
    формула, которая считает не то, поэтому проверяется и чётность.
    """
    out = set()
    for m in re.finditer(r"\bLET\(", formula):
        args = split_args(call_body(formula, m.start()))
        assert len(args) % 2 == 1, f"в LET нечётное число пар: {args[:3]}"
        out |= {arg.strip() for arg in args[:-1:2]}
    return out


def names(formula: str) -> set[str]:
    """Имена, которые формула читает: всё, за чем не стоит скобка."""
    text = bare(formula)
    return {
        m.group(0)
        for m in NAME_RE.finditer(text)
        if text[m.end():m.end() + 1] != "("
    }


def defined(formula: str) -> set[str]:
    """Имена, которые формула сама же и объявила: LET и параметры LAMBDA."""
    text = bare(formula)
    return (
        let_names(text)
        | set(re.findall(r"LAMBDA\(([A-Za-z_][A-Za-z0-9_]*),", text))
        | {"TRUE", "FALSE"}
    )


def balanced(formula: str) -> bool:
    """Скобки и кавычки закрыты. Внутри литерала кавычка удваивается."""
    depth, inside, i = 0, False, 0
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


def every_formula() -> dict[str, str]:
    """Все формулы проекта по одной на имя — так видно, какая именно упала."""
    out = {
        "эффективность": pl.efficiency_formula(PLACEMENT_LAST, OUTREACH_LAST),
        "подсветка нуля": pl.ZERO_RULE,
        "подсветка A": pl.HAS_A_RULE,
        "ссылка размещения": pl.placement_link_formula(2),
        "откликов на размещение": pl.placement_responses_formula(2),
        "метка касаний": pl.outreach_label_formula(2),
        "ссылка касаний": pl.outreach_link_formula(2),
        "откликов из касаний": pl.outreach_responses_formula(2),
    }
    return out


FORMULAS = every_formula()
CASES = list(FORMULAS.items())
IDS = list(FORMULAS)


@pytest.mark.parametrize("name,formula", CASES, ids=IDS)
def test_formula_is_syntactically_whole(name, formula):
    assert formula.startswith("="), formula
    assert balanced(formula), formula


@pytest.mark.parametrize("name,formula", CASES, ids=IDS)
def test_formula_calls_only_real_functions(name, formula):
    unknown = functions(formula) - KNOWN
    assert not unknown, f"нет таких функций в Sheets: {sorted(unknown)}"


@pytest.mark.parametrize("name,formula", CASES, ids=IDS)
def test_formula_reads_only_names_it_defined(name, formula):
    missing = names(formula) - defined(formula)
    assert not missing, f"имя не объявлено в LET: {sorted(missing)}"


def test_names_are_defined_before_they_are_read():
    """LET читает имена сверху вниз.

    Имя, объявленное ниже того места, где его используют, для Sheets не
    существует — вся сводка превратится в ошибку. Проверка прямая: каждое
    значение видит только то, что объявлено до него.
    """
    bindings = pl.efficiency_bindings(PLACEMENT_LAST, OUTREACH_LAST)
    seen: set[str] = set()
    for name, value in bindings:
        used = names(value) - defined(value)
        unseen = used - seen
        assert not unseen, f"{name} читает имя раньше объявления: {sorted(unseen)}"
        seen.add(name)

    body_used = names(pl.efficiency_body()) - defined(pl.efficiency_body())
    assert not body_used - seen, sorted(body_used - seen)


def test_every_let_name_is_used():
    """Неиспользованное имя — обычно след переименования, за которым
    осталась формула, считающая не то."""
    formula = pl.efficiency_formula(PLACEMENT_LAST, OUTREACH_LAST)
    for name, _ in pl.efficiency_bindings(PLACEMENT_LAST, OUTREACH_LAST):
        rest = formula.split(f"{name},", 1)[1]
        assert re.search(rf"\b{name}\b", bare(rest)), f"имя {name} объявлено и забыто"


# ------------------------------- раскладка --------------------------------

def test_manual_columns_come_first():
    """В ручных колонках работает человек — они должны быть слева."""
    assert pl.PLACEMENTS_HEADER[:2] == ["Дата", "Канал"]
    assert pl.PLACEMENT_LINK_COL == 3 and pl.PLACEMENT_RESPONSES_COL == 4
    assert pl.OUTREACH_HEADER[:2] == ["Дата", "Отправлено"]
    assert pl.OUTREACH_LABEL_COL == 3


def test_channels_sheet_is_a_reference_table():
    assert pl.CHANNELS_HEADER == ["Название", "Ссылка", "Тип площадки"]
    assert pl.CHANNEL_NAME_COL == 1, "по названию канал выбирается в «Размещениях»"


def test_widths_are_given_for_every_column():
    assert len(pl.CHANNEL_WIDTHS) == len(pl.CHANNELS_HEADER)
    assert len(pl.PLACEMENT_WIDTHS) == len(pl.PLACEMENTS_HEADER)
    assert len(pl.OUTREACH_WIDTHS) == len(pl.OUTREACH_HEADER)
    assert len(pl.EFFICIENCY_WIDTHS) == pl.EFFICIENCY_COLS


def test_efficiency_has_room_for_the_whole_array():
    """Массиву в A2 некуда расти: не хватит строк — Sheets покажет ошибку
    вместо всей сводки."""
    blocks = pl.PLACEMENT_ROWS + pl.CHANNEL_ROWS
    assert pl.EFFICIENCY_ROWS > 1 + blocks, "одни размещения уже не влезают"


# --------------------------- «Размещения» ---------------------------------

def test_placement_link_carries_the_label_of_its_own_row():
    for row in (2, 7, 100, 201):
        link = pl.placement_link_formula(row)
        assert f"{FORM_DOMAIN}?src={source.label(row)}" in link, row
        # Ссылка пустая, пока строка не заполнена: пустых ссылок в посты
        # не вставляют.
        assert f'IF(AND($A{row}="",$B{row}=""),""' in link


def test_placement_link_does_not_renumber_itself():
    """Метка подставлена текстом, а не посчитана от ROW().

    Формула от ROW() переписала бы метку при вставке строки — и ссылка,
    уже ушедшая в пост, стала бы считаться другому каналу.
    """
    assert "ROW()" not in pl.placement_link_formula(2)


def test_placement_responses_count_the_label_in_the_data_sheet():
    formula = pl.placement_responses_formula(7)
    letter = col_letter(index_of("source"))
    assert f"'Данные'!${letter}$2:${letter}" in formula
    assert f'"{source.label(7)}"' in formula
    assert f'IF($C7="",""' in formula, "без ссылки ячейка пустая, а не ноль"


def test_placements_cover_every_prepared_row():
    values = pl.placements_values(PLACEMENT_LAST)
    assert len(values) == pl.PLACEMENT_ROWS
    assert all(len(row) == 2 for row in values)
    assert source.label(PLACEMENT_LAST) in values[-1][0]


def test_channel_is_chosen_from_the_reference_sheet():
    letter = col_letter(pl.CHANNEL_NAME_COL)
    assert pl.channel_source(101) == f"='{CHANNELS_SHEET}'!${letter}$2:${letter}$101"


# ------------------------- «Личные касания» -------------------------------

def test_outreach_label_is_built_from_the_date():
    formula = pl.outreach_label_formula(4)
    assert f'TEXT($A4,"{pl.sheets_outreach_format()}")' in formula
    assert f'"{source.OUTREACH_PREFIX}"' in formula
    # Дата, введённая текстом, даёт пустую метку, а не #VALUE! во всю колонку.
    assert "IFERROR(" in formula


def test_sheets_date_format_matches_python():
    """«lk_16_10» должен получаться одинаково — и в таблице, и в отчёте."""
    assert source.OUTREACH_DATE_FORMAT == "%d_%m"
    assert pl.sheets_outreach_format() == "dd_mm"


def test_outreach_link_and_count_follow_the_label_cell():
    letter = col_letter(pl.OUTREACH_LABEL_COL)
    link = pl.outreach_link_formula(9)
    assert f'"{FORM_DOMAIN}?src="&${letter}9' in link

    count = pl.outreach_responses_formula(9)
    assert f"COUNTIF('Данные'!" in count
    assert f"${letter}9)" in count, "считается метка этой же строки"


def test_outreach_is_one_row_per_day():
    values = pl.outreach_values(1 + pl.OUTREACH_ROWS)
    assert len(values) == pl.OUTREACH_ROWS
    assert pl.OUTREACH_ROWS > 250, "строка на день — минимум на год вперёд"


# --------------------------- «Эффективность» ------------------------------

@pytest.fixture(scope="module")
def eff():
    return pl.efficiency_formula(PLACEMENT_LAST, OUTREACH_LAST)


@pytest.fixture(scope="module")
def binding():
    return dict(pl.efficiency_bindings(PLACEMENT_LAST, OUTREACH_LAST))


def data_column(name: str) -> str:
    letter = col_letter(index_of(name))
    return f"'Данные'!${letter}${FIRST_DATA_ROW}:${letter}"


@pytest.mark.parametrize(
    "name,column",
    [
        ("src", "source"),
        ("ver", "verdict"),
        ("tot", "total_score"),
        ("cnd", "conditions_ru"),
        ("idc", "id"),
    ],
)
def test_summary_looks_at_the_right_data_columns(binding, name, column):
    assert binding[name] == data_column(column)


def test_placement_ranges_are_bounded(binding):
    """По высоте этого диапазона считаются номера строк, то есть метки."""
    for name, col in (("pd", pl.PLACEMENT_DATE_COL), ("pc", pl.PLACEMENT_CHANNEL_COL)):
        letter = col_letter(col)
        assert binding[name] == (
            f"'{PLACEMENTS_SHEET}'!${letter}$2:${letter}${PLACEMENT_LAST}"
        )


def test_labels_in_the_summary_match_the_links_in_the_sheet(binding):
    """Метка сводки и метка ссылки — одна и та же строка.

    В сводке она считается из номера строки формулой, в «Размещениях» —
    подставлена текстом при сборке. Разойдясь, они дали бы сводку, в которой
    у каждого размещения ноль откликов.
    """
    assert f'SEQUENCE(ROWS(pd),1,{FIRST_DATA_ROW})' in binding["pl"]
    assert f'"{source.LABEL_PREFIX}"' in binding["pl"]
    assert f'TEXT(r,"{"0" * source.LABEL_DIGITS}")' in binding["pl"]
    assert source.label(2) == "p002", "формат метки изменился — проверьте формулу"


def test_rows_without_date_and_channel_are_skipped(binding):
    assert binding["use"].startswith("FILTER(HSTACK(pd,pc,pl)")
    assert '(pd<>"")+(pc<>"")' in binding["use"]


def test_verdict_counts_split_a_b_c(binding):
    assert 'COUNTIFS(src,l,ver,"A")' in binding["na"]
    assert 'COUNTIFS(src,l,ver,"B")' in binding["nb"]
    assert 'COUNTIFS(src,l,ver,"C")' in binding["nc"]


def test_stop_failures_are_counted_by_the_conditions_column(binding):
    assert binding["rej"] == f'"{ru.REJECTED_PREFIX}*"'
    assert "COUNTIFS(src,l,cnd,rej)" in binding["ns"]


def test_average_score_ignores_those_cut_off_by_stop_questions(binding):
    """Отсеявшиеся до баллов не доходят: в знаменателе их быть не должно."""
    assert "COUNTIF(src,l)-COUNTIFS(src,l,cnd,rej)" in binding["avg"]
    assert 'IF(k=0,""' in binding["avg"], "ноль оценённых — пустая ячейка, не ошибка"
    assert "SUMIF(src,l,tot)/k" in binding["avg"]


def test_channel_block_counts_placements_and_their_yield(binding):
    assert binding["chan"] == 'UNIQUE(FILTER(pc,pc<>""))'
    assert binding["cp"] == "MAP(chan,LAMBDA(c,COUNTIF(pc,c)))"
    assert "SUM(MAP(FILTER(pl,pc=c)" in binding["cr"]


def test_channel_block_compares_the_first_placement_with_the_last(binding):
    """Выдохшийся канал виден по тому, что последнее размещение дало меньше."""
    assert "SORT(d,1,TRUE)" in binding["cf"], "первое — самая ранняя дата"
    assert "SORT(d,1,FALSE)" in binding["cl"], "последнее — самая поздняя"
    for name in ("cf", "cl"):
        assert '(pc=c)*(pd<>"")' in binding[name], "без даты размещения не было"
        assert 'IFERROR(' in binding[name]


def test_unknown_labels_are_those_no_placement_claims(binding):
    assert "COUNTIF(lab,s)=0" in binding["unk"]
    assert 's=""' in binding["unk"], "отклик без метки — не чужая метка"
    assert binding["none"] == 'SUMPRODUCT((idc<>"")*(src=""))'


def test_outreach_labels_are_not_called_strangers(binding):
    """Метку lk_ выдаёт лист «Личные касания». Размещения у неё нет по
    определению, и в блок чужих меток она попадать не должна."""
    assert f"LEFT(s,{len(source.OUTREACH_PREFIX)})=lkp" in binding["unk"]
    assert binding["lkp"] == f'"{source.OUTREACH_PREFIX}"'


def test_outreach_block_counts_sent_and_answered(binding):
    assert binding["osent"] == "SUM(os)"
    assert binding["os"].startswith(f"'{pl.OUTREACH_SHEET}'!")
    assert f"LEFT(src,{len(source.OUTREACH_PREFIX)})=lkp" in binding["lkn"]


def test_summary_is_sorted_by_date_newest_first(eff):
    assert ",1,FALSE)" in eff
    block = eff[eff.index("SORT(HSTACK(CHOOSECOLS(use,1)"):]
    assert block.startswith("SORT(HSTACK(CHOOSECOLS(use,1),CHOOSECOLS(use,2),n,")


def test_blocks_go_in_the_agreed_order(eff):
    positions = [
        eff.index("SORT(HSTACK(CHOOSECOLS"),
        eff.index(pl.CHANNELS_BLOCK_TITLE),
        eff.index(pl.OUTREACH_BLOCK_TITLE),
        eff.index(pl.UNKNOWN_BLOCK_TITLE),
        eff.index(pl.NO_LABEL_TITLE),
    ]
    assert positions == sorted(positions), "блоки сводки идут не в том порядке"


def test_empty_blocks_say_so_instead_of_breaking(eff):
    for text in (pl.NO_PLACEMENTS_TEXT, pl.UNKNOWN_EMPTY_TEXT):
        assert f'"{text}"' in eff
    assert 'IFNA(VSTACK(' in eff, "блоки разной ширины добиваются пустотой"


def test_header_has_a_column_for_every_number(eff):
    assert pl.EFFICIENCY_HEADER[:3] == ["Дата", "Канал", "Откликов"]
    assert pl.EFFICIENCY_COLS == 8
    assert len(pl.CHANNELS_BLOCK_HEADER) <= pl.EFFICIENCY_COLS


# ------------------------------- подсветка --------------------------------

def test_zero_rows_are_gray_and_rows_with_a_are_green():
    assert pl.ZERO_RULE == "=AND(ISNUMBER($C2),$C2=0)"
    assert pl.HAS_A_RULE == "=AND(ISNUMBER($A2),ISNUMBER($D2),$D2>0)"


def test_color_rules_cover_the_whole_summary_and_nothing_above_it():
    rules = pl.color_rules(sheet_id=7, last_row=600)
    assert len(rules) == 2
    assert [r["addConditionalFormatRule"]["index"] for r in rules] == [0, 1]
    for request in rules:
        grid = request["addConditionalFormatRule"]["rule"]["ranges"][0]
        assert grid["sheetId"] == 7
        assert grid["startRowIndex"] == 1, "шапку красить не нужно"
        assert grid["endRowIndex"] == 600
        assert grid["endColumnIndex"] == pl.EFFICIENCY_COLS

    formulas = [
        r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0][
            "userEnteredValue"
        ]
        for r in rules
    ]
    assert formulas == [pl.HAS_A_RULE, pl.ZERO_RULE], (
        "зелёное правило должно стоять выше серого — побеждает первое"
    )


def test_titles_never_fall_under_the_coloring_rules():
    """Подписи блоков стоят в тех же колонках, что числа. Правило смотрит на
    ISNUMBER — иначе серыми стали бы и заголовки."""
    assert "ISNUMBER($C2)" in pl.ZERO_RULE
    assert "ISNUMBER($A2)" in pl.HAS_A_RULE, "в блоке по каналам в A — название"


# ------------------------------- примечания -------------------------------

def test_notes_warn_about_inserting_rows():
    note = pl.NOTES[PLACEMENTS_SHEET]["C1"]
    assert "вниз" in note and "сдвигает" in note

def test_notes_explain_the_manual_columns():
    assert "списка" in pl.NOTES[PLACEMENTS_SHEET]["B1"]
    assert "датой" in pl.NOTES[OUTREACH_SHEET]["A1"]
