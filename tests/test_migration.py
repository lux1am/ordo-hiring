"""Скрипты, которые ходят в книгу: архив, шапка, карточка, учёт размещений.

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
import digest
import placements
import sync_questions
from HEADERS import FIRST_DATA_ROW, HEADERS, col_letter

# Прежняя раскладка: те же колонки, другие имена стоп-вопросов.
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
        self.notes: dict[str, str] = {}
        self.formats: list = []
        self.conditional: list = []
        self.frozen = 0
        # Способ ввода по диапазону: формула, записанная как RAW, легла бы
        # в ячейку текстом, и лист показывал бы саму формулу.
        self.input_options: dict[str, str] = {}
        self.hidden = False
        self.appends = 0
        self.id = spreadsheet.next_id() if spreadsheet else 0
        self.widths: dict[int, int] = {}
        self.validations: list[tuple] = []

    # --- служебное -------------------------------------------------------
    def called(self) -> None:
        """Одно обращение к API. Их считает книга: у Sheets лимит на минуту."""
        if self.spreadsheet is not None:
            self.spreadsheet.writes += 1

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

    def get_values(self, value_render_option=None) -> list[list[str]]:
        """Так читают листы учёта: целиком и без форматирования.

        Чтения, как и в остальном листе в памяти, в счётчик обращений не
        идут: у Sheets свой лимит на чтение, и упираемся мы не в него.
        """
        return self.get_all_values()

    def row_values(self, row: int) -> list[str]:
        cols = max((c for r, c in self.cells if r == row), default=0)
        return [self.cells.get((row, c), "") for c in range(1, cols + 1)]

    def update(self, values, range_name, value_input_option="RAW"):
        self.input_options[range_name] = value_input_option
        self.called()
        top, left, _, _ = parse_range(range_name)
        for dr, line in enumerate(values):
            for dc, value in enumerate(line):
                self.put(top + dr, left + dc, value)

    def append_rows(self, values, value_input_option="RAW"):
        self.appends += 1
        self.called()
        start = self.last_row() + 1
        for dr, line in enumerate(values):
            for dc, value in enumerate(line):
                self.put(start + dr, 1 + dc, value)

    def batch_clear(self, ranges):
        self.called()
        for a1 in ranges:
            top, left, bottom, right = parse_range(a1)
            for r in range(top, bottom + 1):
                for c in range(left, right + 1):
                    self.cells.pop((r, c), None)

    def hide(self):
        self.hidden = True

    def show(self):
        self.hidden = False

    @property
    def isSheetHidden(self) -> bool:
        return self.hidden

    def update_notes(self, notes):
        """Пустой текст стирает примечание — так же ведёт себя Sheets."""
        self.called()
        for cell, text in notes.items():
            if text:
                self.notes[cell] = text
            else:
                self.notes.pop(cell, None)

    def resize(self, rows, cols):
        """Ужимая лист, Sheets теряет всё, что осталось за границей."""
        self.called()
        self.row_count, self.col_count = rows, cols
        self.cells = {
            (r, c): v for (r, c), v in self.cells.items() if r <= rows and c <= cols
        }

    def batch_format(self, formats):
        self.called()
        self.formats.extend(formats)

    def format_of(self, range_name: str) -> dict:
        """Последний формат, заданный этому диапазону."""
        found = [f["format"] for f in self.formats if f["range"] == range_name]
        return found[-1] if found else {}

    def add_validation(self, range, condition_type, values, **kw):
        self.called()
        self.validations.append((range, condition_type, tuple(values), kw))


class FakeBook:
    def __init__(self):
        self.sheets: dict[str, FakeWorksheet] = {}
        self._ids = 0
        self.writes = 0

    def next_id(self) -> int:
        """Идентификаторы не переиспользуются: удалённый лист не отдаёт свой."""
        self._ids += 1
        return self._ids

    def by_id(self, sheet_id: int) -> FakeWorksheet:
        return next(ws for ws in self.sheets.values() if ws.id == sheet_id)

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
        self.writes += 1
        return self.add(title, rows=rows, cols=cols)

    def worksheets(self):
        return list(self.sheets.values())

    def del_worksheet(self, ws):
        self.sheets.pop(ws.title)

    def fetch_sheet_metadata(self, params=None):
        """Столько, сколько читает код: id листа и его правила подсветки."""
        return {
            "sheets": [
                {
                    "properties": {"sheetId": ws.id, "title": ws.title},
                    "conditionalFormats": list(ws.conditional),
                }
                for ws in self.sheets.values()
            ]
        }

    def batch_update(self, body):
        """Применяет то, чем пользуется код: видимость, размеры, подсветку."""
        self.writes += 1
        self.requests = body["requests"]
        for request in self.requests:
            props = request.get("updateSheetProperties", {}).get("properties")
            if props:
                ws = self.by_id(props["sheetId"])
                if "hidden" in props:
                    ws.hidden = props["hidden"]
                grid = props.get("gridProperties", {})
                if "frozenRowCount" in grid:
                    ws.frozen = grid["frozenRowCount"]
            dimension = request.get("updateDimensionProperties")
            if dimension:
                ws = self.by_id(dimension["range"]["sheetId"])
                ws.widths[dimension["range"]["startIndex"]] = (
                    dimension["properties"]["pixelSize"]
                )
            add = request.get("addConditionalFormatRule")
            if add:
                ws = self.by_id(add["rule"]["ranges"][0]["sheetId"])
                ws.conditional.insert(add["index"], add["rule"])
            drop = request.get("deleteConditionalFormatRule")
            if drop:
                self.by_id(drop["sheetId"]).conditional.pop(drop["index"])


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

    for module in (archive_old, apply_headers, sync_questions):
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
    # Правый диапазон доходит до BC: метка размещения тоже должна уехать в
    # архив, иначе старый отклик остался бы приписан будущей ссылке.
    assert ranges == ["A2:AM300", "AO2:BC300"]
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


def test_appended_column_is_written_over_live_rows(book):
    """Колонка, дописанная в конец, не меняет смысла прежних строк.

    Так в лист приехала метка размещения. Гнать анкеты живых людей в архив
    ради новой колонки незачем, и --force для этого не нужен.
    """
    data = data_sheet(book)
    # Прежняя шапка без последней колонки — ровно то, что было в книге до
    # появления метки.
    data.update(values=[HEADERS[:-1]], range_name="A1")
    data.put(1, len(HEADERS), "")

    assert apply_headers.main([]) == 0
    assert data.row_values(1) == HEADERS
    assert len(archive_old.data_rows(data.get_all_values())) == 3, "строки на месте"
    assert archive_old.ARCHIVE_TITLE not in book.sheets, "архив не понадобился"


def test_renamed_column_still_demands_the_archive(book):
    """Переименование — другое дело: ячейка под новым именем означала бы не
    то, что в ней написано."""
    assert apply_headers.main([]) == 1
    assert data_sheet(book).row_values(1) == OLD_HEADERS


def test_appended_only_looks_at_the_tail():
    short = HEADERS[:-1]
    assert apply_headers.appended_only(
        apply_headers.header_diff(short), short
    ) is True

    renamed = list(HEADERS)
    renamed[6] = "stop_1_oklad"
    assert apply_headers.appended_only(
        apply_headers.header_diff(renamed), renamed
    ) is False
    # Пустая шапка — это не «дописали в конец», это несобранный лист.
    assert apply_headers.appended_only(apply_headers.header_diff([]), []) is False


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


# ------------------------------ карточка ----------------------------------
#
# Скрипт шапки тянет за собой sync_questions: формулировки вопросов в
# карточке меняются вместе с составом колонок, и отставать они не должны.


def card_sheet(book) -> FakeWorksheet:
    return book.sheets[sync_questions.CARD_TITLE]


def add_hand_made_sheets(book) -> None:
    """Листы, собранные в книге руками: скрипт их не создаёт, только показывает."""
    for title in ("Кандидаты", "Как читать"):
        book.add(title, rows=10, cols=13).hidden = True


def test_headers_bring_the_card_along(book):
    archive_old.main([])
    assert apply_headers.main([]) == 0

    values = card_sheet(book).get_all_values()
    assert values[0][0] == "", "в A1 ложится выбранная фамилия"
    assert any(row[0] == sync_questions.STOPS_TITLE for row in values)
    assert any(
        "За день вы получили 20 отказов подряд" in row[0] for row in values
    ), "формулировки вопросов записаны текстом"


def test_card_replaces_the_old_sheet_and_its_notes(book):
    """Прежняя затея — лист «Вопросы» и примечания к шапке — убирается."""
    data = data_sheet(book)
    book.add(sync_questions.OLD_SHEET_TITLE, rows=40, cols=3)
    data.notes["M1"] = "Вопрос 1: за день вы получили 20 отказов"
    data.notes["G1"] = "Стоп-вопрос 1"

    archive_old.main([])
    assert sync_questions.main([]) == 0

    assert sync_questions.OLD_SHEET_TITLE not in book.sheets
    assert data.notes == {}, "примечания сняты со всей шапки"


def test_card_is_idempotent(book):
    archive_old.main([])
    assert sync_questions.main([]) == 0
    first = card_sheet(book).get_all_values()

    assert sync_questions.main([]) == 0
    assert card_sheet(book).get_all_values() == first
    assert sorted(book.sheets) == sorted(
        [
            "Данные",
            archive_old.ARCHIVE_TITLE,
            sync_questions.CARD_TITLE,
            digest.DIGEST_SHEET,
            *PLACEMENT_SHEETS,
        ]
    ), "повторный запуск завёл лишний лист"


def test_card_keeps_the_chosen_candidate(book):
    """Пересборка не должна сбрасывать карточку, открытую на кандидате."""
    archive_old.main([])
    sync_questions.main([])
    card_sheet(book).put(1, 1, "Кандидат 1")

    assert sync_questions.main([]) == 0
    assert card_sheet(book).get_all_values()[0][0] == "Кандидат 1"


def test_card_shrinks_instead_of_keeping_old_rows(book):
    archive_old.main([])
    sync_questions.main([])
    rows = len(card_sheet(book).get_all_values())

    card_sheet(book).update(values=[["мусор"]], range_name=f"A{rows + 5}")
    assert sync_questions.main([]) == 0
    assert len(card_sheet(book).get_all_values()) == rows


def test_card_gets_the_name_dropdown_and_widths(book):
    archive_old.main([])
    sync_questions.main([])
    card = card_sheet(book)

    cell, condition, values, kw = card.validations[-1]
    assert cell == "A1"
    assert values == (sync_questions.name_source(),)
    assert kw["showCustomUi"] is True, "без этого выпадающего списка не будет"
    assert card.widths == dict(enumerate(sync_questions.column_widths()))


def test_visibility_is_restored(book):
    add_hand_made_sheets(book)
    archive_old.main([])
    assert sync_questions.main([]) == 0

    assert data_sheet(book).hidden is True
    assert archive_sheet(book).hidden is True
    for title in sync_questions.VISIBLE_SHEETS:
        assert book.sheets[title].hidden is False, title


def test_missing_hand_made_sheets_do_not_break_the_run(book):
    """«Кандидаты» и «Как читать» собраны руками — в чужой копии их может не быть."""
    archive_old.main([])
    assert sync_questions.main([]) == 0
    assert card_sheet(book).hidden is False
    assert data_sheet(book).hidden is True


def test_card_failure_does_not_undo_the_header(book):
    """Шапка к этому моменту записана и перечитана — прогон не проваливается."""
    archive_old.main([])
    data = data_sheet(book)
    data.update_notes = lambda notes: (_ for _ in ()).throw(RuntimeError("нет прав"))

    assert apply_headers.main([]) == 0
    assert data.row_values(1) == HEADERS
    assert sync_questions.CARD_TITLE not in book.sheets


def test_card_dry_run_touches_nothing(book):
    archive_old.main([])
    assert apply_headers.main(["--dry-run"]) == 0
    assert sync_questions.CARD_TITLE not in book.sheets

    assert sync_questions.main(["--dry-run"]) == 0
    assert sync_questions.CARD_TITLE not in book.sheets
    assert data_sheet(book).notes == {}


# ------------------------- учёт размещений --------------------------------
#
# Четыре листа, в двух из которых работает человек. Главное, что здесь
# проверяется: повторный запуск не трогает заполненное руками и не плодит
# ни листов, ни правил подсветки.

PLACEMENT_SHEETS = (
    placements.CHANNELS_SHEET,
    placements.PLACEMENTS_SHEET,
    placements.OUTREACH_SHEET,
    placements.EFFICIENCY_SHEET,
)


def placement_sheet(book) -> FakeWorksheet:
    return book.sheets[placements.PLACEMENTS_SHEET]


def test_all_four_sheets_appear(book):
    assert sync_questions.main([]) == 0
    for title in PLACEMENT_SHEETS:
        assert title in book.sheets, title
        assert book.sheets[title].cells.get((1, 1)), f"{title}: пустая шапка"


def test_headers_are_written_in_the_agreed_order(book):
    sync_questions.main([])
    for title, header in (
        (placements.CHANNELS_SHEET, placements.CHANNELS_HEADER),
        (placements.PLACEMENTS_SHEET, placements.PLACEMENTS_HEADER),
        (placements.OUTREACH_SHEET, placements.OUTREACH_HEADER),
        (placements.EFFICIENCY_SHEET, placements.EFFICIENCY_HEADER),
    ):
        assert book.sheets[title].row_values(1) == header, title


def test_formula_columns_are_filled_down_the_sheet(book):
    sync_questions.main([])
    ws = placement_sheet(book)
    last = ws.row_count

    assert last == 1 + placements.PLACEMENT_ROWS
    for row in (FIRST_DATA_ROW, last):
        assert ws.cells[(row, placements.PLACEMENT_LINK_COL)] == (
            placements.placement_link_formula(row)
        )
        assert ws.cells[(row, placements.PLACEMENT_RESPONSES_COL)] == (
            placements.placement_responses_formula(row)
        )


def test_formulas_are_written_as_formulas(book):
    """RAW положил бы в ячейку текст формулы, и лист показывал бы его."""
    sync_questions.main([])
    # «Каналы» — справочник без формул: там шапка и ручной ввод.
    with_formulas = [t for t in PLACEMENT_SHEETS if t != placements.CHANNELS_SHEET]
    for title in with_formulas:
        options = book.sheets[title].input_options
        assert "USER_ENTERED" in options.values(), title
        header = f"A1:{col_letter(book.sheets[title].col_count)}1"
        assert options.get(header, "RAW") == "RAW", f"{title}: шапка — обычный текст"


def test_manual_columns_survive_a_rebuild(book):
    """В «Размещениях» и «Личных касаниях» работает человек. Пересборка
    переписывает только шапку и формулы — иначе она стирала бы его работу."""
    sync_questions.main([])
    ws = placement_sheet(book)
    ws.put(2, placements.PLACEMENT_DATE_COL, "2026-10-12")
    ws.put(2, placements.PLACEMENT_CHANNEL_COL, "Авито")

    outreach = book.sheets[placements.OUTREACH_SHEET]
    outreach.put(2, placements.OUTREACH_DATE_COL, "2026-10-12")
    outreach.put(2, placements.OUTREACH_SENT_COL, "20")

    channels = book.sheets[placements.CHANNELS_SHEET]
    channels.put(2, 1, "Авито")
    channels.put(2, 2, "https://avito.ru/vacancy")
    channels.put(2, 3, "доска вакансий")

    assert sync_questions.main([]) == 0

    assert ws.cells[(2, placements.PLACEMENT_DATE_COL)] == "2026-10-12"
    assert ws.cells[(2, placements.PLACEMENT_CHANNEL_COL)] == "Авито"
    assert outreach.cells[(2, placements.OUTREACH_SENT_COL)] == "20"
    assert channels.cells[(2, 2)] == "https://avito.ru/vacancy"


def test_existing_sheet_is_never_shrunk(book):
    """Лист, заведённый руками, шире и длиннее размеченного. Ужать его
    значило бы стереть то, что Ника успела в него завести."""
    big = 1 + placements.PLACEMENT_ROWS + 500
    book.add(placements.PLACEMENTS_SHEET, rows=big, cols=8)
    book.sheets[placements.PLACEMENTS_SHEET].put(big - 10, 1, "2026-12-31")

    assert sync_questions.main([]) == 0
    ws = placement_sheet(book)
    assert ws.row_count == big
    assert ws.cells[(big - 10, 1)] == "2026-12-31"
    assert ws.cells[(big, placements.PLACEMENT_LINK_COL)] == (
        placements.placement_link_formula(big)
    ), "формулы должны дойти до конца листа, а не до размеченной строки"


def test_short_sheet_grows_to_the_prepared_size(book):
    book.add(placements.OUTREACH_SHEET, rows=3, cols=2)

    assert sync_questions.main([]) == 0
    ws = book.sheets[placements.OUTREACH_SHEET]
    assert ws.row_count == 1 + placements.OUTREACH_ROWS
    assert ws.col_count >= len(placements.OUTREACH_HEADER)


def test_channel_is_a_dropdown_from_the_channels_sheet(book):
    sync_questions.main([])
    cell, condition, values, kw = placement_sheet(book).validations[-1]

    letter = col_letter(placements.PLACEMENT_CHANNEL_COL)
    assert cell == f"{letter}{FIRST_DATA_ROW}:{letter}{1 + placements.PLACEMENT_ROWS}"
    assert values == (placements.channel_source(1 + placements.CHANNEL_ROWS),)
    assert kw["strict"] is True, "свободный ввод развалил бы сводку по каналу"
    assert kw["showCustomUi"] is True


def test_efficiency_is_one_formula_in_a2(book):
    sync_questions.main([])
    ws = book.sheets[placements.EFFICIENCY_SHEET]

    assert ws.cells[(2, 1)].startswith("=LET(")
    assert ws.input_options["A2"] == "USER_ENTERED"
    # Ниже и правее формулы ничего быть не должно: массив разворачивается
    # сам, и любая занятая ячейка под ним даёт ошибку вместо сводки.
    assert [rc for rc in ws.cells if rc[0] > 2] == []


def test_efficiency_formula_knows_the_real_size_of_the_other_sheets(book):
    """Границы диапазонов должны совпадать с настоящими листами: по высоте
    «Размещений» считаются метки, а разъехавшись, сводка посчитает чужие."""
    posts, touches = 1 + placements.PLACEMENT_ROWS + 7, 1 + placements.OUTREACH_ROWS + 3
    book.add(placements.PLACEMENTS_SHEET, rows=posts, cols=4)
    book.add(placements.OUTREACH_SHEET, rows=touches, cols=5)
    sync_questions.main([])

    formula = book.sheets[placements.EFFICIENCY_SHEET].cells[(2, 1)]
    assert formula == placements.efficiency_formula(posts, touches)


def test_coloring_rules_are_set_once_and_not_piled_up(book):
    sync_questions.main([])
    ws = book.sheets[placements.EFFICIENCY_SHEET]
    assert len(ws.conditional) == 2

    assert sync_questions.main([]) == 0
    assert len(ws.conditional) == 2, "каждый запуск добавлял бы ещё два правила"

    formulas = [
        rule["booleanRule"]["condition"]["values"][0]["userEnteredValue"]
        for rule in ws.conditional
    ]
    assert formulas == [placements.HAS_A_RULE, placements.ZERO_RULE]


def test_headers_are_frozen_and_bold(book):
    sync_questions.main([])
    for title in PLACEMENT_SHEETS:
        ws = book.sheets[title]
        assert ws.frozen == 1, title
        last = col_letter(ws.col_count if title != placements.EFFICIENCY_SHEET
                          else placements.EFFICIENCY_COLS)
        assert ws.format_of(f"A1:{last}1").get("textFormat") == {"bold": True}, title


def test_dates_are_formatted_as_dates(book):
    """Иначе в колонке стояли бы числа вроде 46 000, а метка дня не собралась."""
    sync_questions.main([])
    ws = book.sheets[placements.OUTREACH_SHEET]
    letter = col_letter(placements.OUTREACH_DATE_COL)
    fmt = ws.format_of(f"{letter}{FIRST_DATA_ROW}:{letter}{ws.row_count}")
    assert fmt["numberFormat"]["type"] == "DATE"


def test_notes_are_put_on_the_headers(book):
    sync_questions.main([])
    notes = placement_sheet(book).notes
    assert "C1" in notes and "вниз" in notes["C1"]


def test_placement_sheets_are_visible(book):
    add_hand_made_sheets(book)
    sync_questions.main([])
    for title in PLACEMENT_SHEETS:
        assert book.sheets[title].hidden is False, title


def test_dry_run_creates_no_placement_sheets(book):
    assert sync_questions.main(["--dry-run"]) == 0
    for title in PLACEMENT_SHEETS:
        assert title not in book.sheets, title


def test_two_runs_in_a_row_fit_the_api_quota(book):
    """Sheets даёт шестьдесят записей в минуту на пользователя.

    Скрипт идемпотентный, и его запускают повторно сразу за первым разом —
    посмотреть, что получилось, и собрать заново. Два прогона подряд не
    должны упираться в 429: на полпути это оставило бы листы недособранными.
    """
    add_hand_made_sheets(book)

    book.writes = 0
    assert sync_questions.main([]) == 0
    first = book.writes

    book.writes = 0
    assert sync_questions.main([]) == 0
    second = book.writes

    assert first <= 35, f"обращений к API за первый прогон: {first}"
    assert second < first, "повторный прогон не должен заводить листы заново"
    assert first + second <= 60, f"два прогона подряд: {first + second}"


def test_formatting_of_a_sheet_is_two_requests(book):
    """Шапка, формат даты, ширина колонок и подсветка идут вместе.

    По отдельности они стоили бы четырёх запросов на лист — шестнадцати на
    четыре листа, и это только оформление.
    """
    sync_questions.main([])
    ws = book.sheets[placements.EFFICIENCY_SHEET]

    ranges = [f["range"] for f in ws.formats]
    assert ranges == [
        f"A1:{col_letter(placements.EFFICIENCY_COLS)}1",
        f"A{FIRST_DATA_ROW}:A{ws.row_count}",
        f"G{FIRST_DATA_ROW}:G{ws.row_count}",
    ], "форматы ячеек должны уйти одним batch_format"


# ------------------------- сводка для бота --------------------------------
#
# Лист «Сводка для Поли» (digest.py) — выгрузка значениями для внешнего
# бота. Собирается скрытым и целиком принадлежит коду: ручного в нём нет,
# поэтому пересборка переписывает его сверху донизу и ужимает до нужного
# размера. Числа проверяются в test_digest.py, здесь — работа с книгой.

def digest_sheet(book) -> FakeWorksheet:
    return book.sheets[digest.DIGEST_SHEET]


def digest_totals(book) -> dict:
    """Блок ИТОГО листом в памяти: «показатель» → «значение»."""
    rows = digest_sheet(book).get_all_values()
    start = next(i for i, row in enumerate(rows) if row[0] == digest.TOTALS_TITLE)
    return {row[0]: row[1] for row in rows[start + 2:] if row[0]}


def test_digest_appears_hidden_and_filled(book):
    add_hand_made_sheets(book)
    assert sync_questions.main([]) == 0

    ws = digest_sheet(book)
    assert ws.hidden is True, "лист читает бот, а не человек"
    assert ws.col_count == digest.DIGEST_COLS
    assert ws.row_values(1)[0] == digest.CHANNELS_TITLE, "блок с первой строки"
    # Три анкеты фикстуры: все уровня A, ни у одной не заполнено «Что было
    # дальше» — ровно то, за чем бот и приходит.
    assert digest_totals(book)[digest.TOTAL_CANDIDATES] == "3"
    assert digest_totals(book)[digest.STRONG_UNTOUCHED] == "3"


def test_digest_holds_values_and_not_a_single_formula(book):
    """Формулу бот прочитать не может: через API ячейка приходит либо её
    текстом, либо последним пересчитанным значением."""
    sync_questions.main([])
    ws = digest_sheet(book)

    assert set(ws.input_options.values()) == {"RAW"}
    assert not [v for v in ws.cells.values() if str(v).startswith("=")]


def test_digest_rows_are_all_the_same_width(book):
    """Короткая строка оставила бы на месте хвоста числа прошлой сборки."""
    sync_questions.main([])
    ws = digest_sheet(book)
    written = [a1 for a1 in ws.input_options if a1 != "A1"]
    assert written == [f"A1:{col_letter(digest.DIGEST_COLS)}{ws.row_count}"]


def test_second_build_leaves_the_digest_to_process_new(book):
    """Готовый лист повторная сборка книги не трогает.

    Обновляет его прогон process_new, то есть не позже чем через десять
    минут, а записи Sheets идут в один лимит на минуту с тремя десятками
    запросов, которыми собираются остальные листы.
    """
    sync_questions.main([])
    ws = digest_sheet(book)
    ws.put(3, 1, "строка прошлой сборки")

    book.writes = 0
    assert sync_questions.main([]) == 0
    assert ws.cells[(3, 1)] == "строка прошлой сборки"


def test_refresh_rewrites_the_digest_and_shrinks_it(book):
    """Строка, оставшаяся от прошлой сборки, уехала бы боту как ещё один
    канал или ещё один день."""
    sync_questions.main([])
    ws = digest_sheet(book)

    tall = ws.row_count + 20
    ws.resize(rows=tall, cols=digest.DIGEST_COLS)
    ws.put(tall, 1, "Канал, которого больше нет")

    digest.refresh()

    assert ws.row_count < tall
    assert (tall, 1) not in ws.cells
    assert ws.row_values(1)[0] == digest.CHANNELS_TITLE


def test_refresh_costs_one_write(book):
    """Прогон process_new идёт каждые десять минут. Сводка обязана стоить
    ему одну запись: лист не растёт, не красится и не переразмечается."""
    sync_questions.main([])

    book.writes = 0
    digest.refresh()
    assert book.writes == 1


def test_dry_run_creates_no_digest(book):
    assert sync_questions.main(["--dry-run"]) == 0
    assert digest.DIGEST_SHEET not in book.sheets


def test_digest_created_by_a_run_is_hidden_right_away(book):
    """Завести лист может и прогон process_new — он приходит раз в десять
    минут, а сборку книги запускают руками и редко."""
    assert digest.DIGEST_SHEET not in book.sheets

    digest.refresh()

    ws = digest_sheet(book)
    assert ws.hidden is True
    assert ws.row_values(1)[0] == digest.CHANNELS_TITLE
