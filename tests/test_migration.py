"""Разовые скрипты смены схемы: архив старых строк и запись шапки.

Оба ходят в таблицу, поэтому здесь поднят лист в памяти — с теми же
операциями, что вызывают скрипты. Проверять их «осторожным повторным
запуском на боевой книге» нельзя: второй такой запуск и есть то, что тест
обязан поймать заранее.
"""
import re

import gspread
import pytest

import apply_headers
import archive_old
from HEADERS import HEADERS, col_letter

# Прежняя раскладка: те же 54 колонки, другие имена стоп-вопросов.
OLD_HEADERS = [
    {
        "stop_1_sdelno": "stop_1_oklad",
        "stop_2_chasy": "stop_2_komissiya",
        "stop_3_svyaz": "stop_3_kasaniya",
        "stop_4_oformlenie": "stop_4_vstrechi",
        "stop_5_tehnika": "stop_5_vedenie",
        "stop_6_zapis": "stop_6_crm",
    }.get(name, name)
    for name in HEADERS
]

FORMULA = "=IF(A2=\"\",\"\",SUM(AI2:AM2))"


def old_row(n: int) -> list[str]:
    """Строка прежней анкеты: id, немного текста и баллы."""
    row = [""] * len(HEADERS)
    row[0] = f"id{n:03d}"
    row[2] = f"Кандидат {n}"
    row[3] = f"@user{n}"
    row[HEADERS.index("q1_text")] = f"Март 2026: {n} / 100000 / 50000 — по акту"
    row[HEADERS.index("verdict")] = "A"
    row[HEADERS.index("status")] = "done"
    return row


# ------------------------------ лист в памяти -----------------------------

CELL_RE = re.compile(r"^([A-Z]+)(\d+)$")


def col_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def parse_range(a1: str) -> tuple[int, int, int, int]:
    """«A2:AM300» → (строка от, колонка от, строка до, колонка до)."""
    left, _, right = a1.partition(":")
    lm = CELL_RE.match(left)
    rm = CELL_RE.match(right) if right else lm
    return int(lm.group(2)), col_num(lm.group(1)), int(rm.group(2)), col_num(rm.group(1))


class FakeWorksheet:
    """Ровно те операции gspread, которыми пользуются оба скрипта."""

    def __init__(self, title, rows=400, cols=len(HEADERS), spreadsheet=None):
        self.title = title
        self.row_count = rows
        self.col_count = cols
        self.spreadsheet = spreadsheet
        self.cells: dict[tuple[int, int], str] = {}
        self.hidden = False
        self.appends = 0

    # --- служебное -------------------------------------------------------
    def put(self, row: int, col: int, value) -> None:
        text = "" if value is None else str(value)
        if text == "":
            self.cells.pop((row, col), None)
        else:
            self.cells[(row, col)] = text

    def last_row(self) -> int:
        return max((r for r, _ in self.cells), default=0)

    def last_col(self) -> int:
        return max((c for _, c in self.cells), default=0)

    # --- API gspread -----------------------------------------------------
    def get_all_values(self) -> list[list[str]]:
        rows, cols = self.last_row(), self.last_col()
        return [
            [self.cells.get((r, c), "") for c in range(1, cols + 1)]
            for r in range(1, rows + 1)
        ]

    def row_values(self, row: int) -> list[str]:
        cols = max((c for r, c in self.cells if r == row), default=0)
        return [self.cells.get((row, c), "") for c in range(1, cols + 1)]

    def update(self, values, range_name, value_input_option="RAW"):
        top, left, _, _ = parse_range(range_name)
        for dr, line in enumerate(values):
            for dc, value in enumerate(line):
                self.put(top + dr, left + dc, value)

    def append_rows(self, values, value_input_option="RAW"):
        self.appends += 1
        start = self.last_row() + 1
        for dr, line in enumerate(values):
            for dc, value in enumerate(line):
                self.put(start + dr, 1 + dc, value)

    def batch_clear(self, ranges):
        for a1 in ranges:
            top, left, bottom, right = parse_range(a1)
            for r in range(top, bottom + 1):
                for c in range(left, right + 1):
                    self.cells.pop((r, c), None)

    def hide(self):
        self.hidden = True


class FakeBook:
    def __init__(self):
        self.sheets: dict[str, FakeWorksheet] = {}

    def add(self, title, **kw) -> FakeWorksheet:
        ws = FakeWorksheet(title, spreadsheet=self, **kw)
        self.sheets[title] = ws
        return ws

    # --- API gspread -----------------------------------------------------
    def worksheet(self, title):
        if title not in self.sheets:
            raise gspread.WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        return self.add(title, rows=rows, cols=cols)


@pytest.fixture
def book(monkeypatch):
    """Книга с листом «Данные» прежней схемы: шапка, три анкеты, формулы."""
    fake = FakeBook()
    data = fake.add("Данные")
    data.update(values=[OLD_HEADERS], range_name="A1")
    for i in range(1, 4):
        data.update(values=[old_row(i)], range_name=f"A{i + 1}")
    # Формула размечена на строки вперёд — её очистка не должна задевать.
    for r in range(2, 12):
        data.put(r, HEADERS.index("total_score") + 1, FORMULA)

    for module in (archive_old, apply_headers):
        monkeypatch.setattr(module.sheets, "worksheet", lambda: data)
        monkeypatch.setattr(module, "missing_secrets", lambda: [])
    return fake


def data_sheet(book) -> FakeWorksheet:
    return book.sheets["Данные"]


def archive_sheet(book) -> FakeWorksheet:
    return book.sheets[archive_old.ARCHIVE_TITLE]


# ------------------------------ archive_old -------------------------------

def test_archive_moves_rows_and_clears_data(book):
    assert archive_old.main([]) == 0

    archive = archive_sheet(book)
    assert archive.get_all_values()[0] == OLD_HEADERS, "шапка прежней раскладки"
    moved = archive_old.data_rows(archive.get_all_values())
    assert [r[0] for r in moved] == ["id001", "id002", "id003"]
    assert moved[0][HEADERS.index("q1_text")].startswith("Март 2026: 1")

    left = data_sheet(book).get_all_values()
    assert left[0] == OLD_HEADERS, "шапка «Данных» остаётся на месте"
    assert archive_old.data_rows(left) == [], "строки должны быть очищены"


def test_archive_keeps_the_total_score_formula(book):
    archive_old.main([])
    data = data_sheet(book)
    column = HEADERS.index("total_score") + 1
    assert data.cells.get((2, column)) == FORMULA, "формула в AN затёрта"
    assert data.cells.get((11, column)) == FORMULA


def test_archive_is_idempotent(book):
    assert archive_old.main([]) == 0
    first = archive_sheet(book).get_all_values()

    assert archive_old.main([]) == 0, "повторный запуск не должен падать"
    assert archive_sheet(book).get_all_values() == first, "строки задвоились"
    assert len(book.sheets) == 2, "создан второй лист архива"


def test_interrupted_run_finishes_the_job(book):
    """Прогон, упавший между переносом и очисткой, дописывает только остаток."""
    data = data_sheet(book)
    boom = False

    def fail_once(ranges):
        nonlocal boom
        if not boom:
            boom = True
            raise RuntimeError("связь оборвалась")
        FakeWorksheet.batch_clear(data, ranges)

    data.batch_clear = fail_once

    assert archive_old.main([]) == 1, "оборванный прогон обязан вернуть ошибку"
    assert len(archive_old.data_rows(archive_sheet(book).get_all_values())) == 3

    assert archive_old.main([]) == 0
    archive = archive_sheet(book)
    assert [r[0] for r in archive_old.data_rows(archive.get_all_values())] == [
        "id001", "id002", "id003"
    ], "остаток дописан, перенесённое не задвоилось"
    assert archive.appends == 1, "второй прогон дописывать было нечего"
    assert archive_old.data_rows(data_sheet(book).get_all_values()) == []


def test_archive_is_hidden(book):
    archive_old.main([])
    assert archive_sheet(book).hidden is True


def test_hiding_failure_does_not_lose_data(book):
    """Невидимость листа — вопрос удобства, а не сохранности анкет."""
    archive_old.main([])
    book.sheets[archive_old.ARCHIVE_TITLE].hide = lambda: (_ for _ in ()).throw(
        RuntimeError("нет прав")
    )
    data_sheet(book).update(values=[old_row(9)], range_name="A2")

    assert archive_old.main([]) == 0
    assert "id009" in [r[0] for r in archive_old.data_rows(
        archive_sheet(book).get_all_values()
    )]


def test_empty_sheet_needs_no_archive(book):
    archive_old.main([])
    book.sheets.pop(archive_old.ARCHIVE_TITLE)

    assert archive_old.main([]) == 0
    assert archive_old.ARCHIVE_TITLE not in book.sheets, "пустой архив заводить незачем"


def test_dry_run_changes_nothing(book):
    before = data_sheet(book).get_all_values()
    assert archive_old.main(["--dry-run"]) == 0
    assert data_sheet(book).get_all_values() == before
    assert archive_old.ARCHIVE_TITLE not in book.sheets


def test_rows_without_id_are_kept_and_not_duplicated(book):
    """Строку могли править руками и стереть id — терять её всё равно нельзя."""
    orphan = [""] * len(HEADERS)
    orphan[2] = "Без идентификатора"
    data_sheet(book).update(values=[orphan], range_name="A5")

    archive_old.main([])
    moved = archive_old.data_rows(archive_sheet(book).get_all_values())
    assert any(r[2] == "Без идентификатора" for r in moved)

    data_sheet(book).update(values=[orphan], range_name="A2")
    archive_old.main([])
    moved = archive_old.data_rows(archive_sheet(book).get_all_values())
    assert sum(1 for r in moved if r[2] == "Без идентификатора") == 1


# --- чистые функции -------------------------------------------------------

def test_clear_ranges_skip_the_formula_column():
    ranges = archive_old.clear_ranges(300)
    assert ranges == ["A2:AM300", "AO2:BB300"]
    assert col_letter(HEADERS.index("total_score") + 1) == "AN"
    assert not any("AN" in r for r in ranges)


def test_clear_ranges_of_empty_sheet():
    assert archive_old.clear_ranges(1) == []


def test_data_rows_skip_blank_lines():
    values = [OLD_HEADERS, old_row(1), [""] * len(HEADERS), ["  ", ""], old_row(2)]
    assert [r[0] for r in archive_old.data_rows(values)] == ["id001", "id002"]


# ----------------------------- apply_headers ------------------------------

def test_headers_are_written_after_archiving(book):
    archive_old.main([])
    assert apply_headers.main([]) == 0
    assert data_sheet(book).row_values(1) == HEADERS


def test_headers_refuse_to_overwrite_live_rows(book):
    """Порядок обязателен: под новой шапкой старые строки означали бы не то."""
    assert apply_headers.main([]) == 1
    assert data_sheet(book).row_values(1) == OLD_HEADERS, "шапка не должна меняться"


def test_force_writes_over_live_rows(book):
    assert apply_headers.main(["--force"]) == 0
    assert data_sheet(book).row_values(1) == HEADERS


def test_headers_are_idempotent(book):
    archive_old.main([])
    assert apply_headers.main([]) == 0
    assert apply_headers.main([]) == 0, "второй запуск не находит расхождений"
    assert data_sheet(book).row_values(1) == HEADERS


def test_headers_dry_run_changes_nothing(book):
    archive_old.main([])
    assert apply_headers.main(["--dry-run"]) == 0
    assert data_sheet(book).row_values(1) == OLD_HEADERS


def test_header_diff_names_the_columns():
    diff = apply_headers.header_diff(OLD_HEADERS)
    assert [i for i, _, _ in diff] == list(range(7, 13)), "разошлись ровно G..L"
    assert diff[0] == (7, "stop_1_sdelno", "stop_1_oklad")
    assert apply_headers.header_diff(HEADERS) == []


def test_header_diff_reports_a_short_row():
    diff = apply_headers.header_diff(["id", "created_at"])
    assert len(diff) == len(HEADERS) - 2
    assert diff[0] == (3, "fio", "")


def test_occupied_rows_ignores_blank_lines():
    values = [HEADERS, old_row(1), [""] * len(HEADERS), old_row(2)]
    assert apply_headers.occupied_rows(values) == 2
    assert apply_headers.occupied_rows([HEADERS]) == 0
