"""Сабмит и обработка на одном фейковом листе.

Зона AO..AZ пишется дважды: /submit кладёт в неё флаги телеметрии (AS) и
статус (AU), обработчик перезаписывает её целиком. Здесь проверяется, что
обработчик действительно вычитывает AS и сливает флаги, а не затирает их.
"""
import json

import pytest
from fastapi.testclient import TestClient

from HEADERS import COL_RESULT_START, HEADERS
from app import main, pipeline
from app.checks import FLAG_MATH, FLAG_MOBILE, FLAG_PASTED
from app.config import STATUS_DONE, STATUS_NEW
from app.models import MODEL_QKEYS, OPEN_FIELDS, STOP_FIELDS, ScorerOutput

# Длиннее самого строгого минимума (150 знаков у вопроса 3), чтобы один и
# тот же текст годился всем пяти полям.
ANSWER = ("Закрытой считал сделку после полной оплаты по акту, а не по "
          "подписанному договору. Переговоры вёл с собственниками и "
          "ИТ-директорами, средний чек около 120 000 рублей, цифры смотрел "
          "в CRM за последний квартал.")


class SharedSheet:
    """Один лист на 54 колонки: в него пишет и /submit, и обработчик."""

    def __init__(self):
        self.cells = [""] * 54
        self.cells[HEADERS.index("total_score")] = "=ФОРМУЛА"
        self.result_writes = 0

    # --- вызывается из main (сабмит) ---
    def allocate_row(self):
        return 2

    def telegram_exists(self, telegram):
        return False

    def write_form(self, row, values, flags, status=STATUS_NEW):
        for i, v in enumerate(values):
            self.cells[i] = v
        self.cells[HEADERS.index("flags")] = flags
        self.cells[HEADERS.index("status")] = status

    # --- вызывается из pipeline (обработка) ---
    def read_status(self, row):
        return self.cells[HEADERS.index("status")]

    def set_status(self, row, status):
        self.cells[HEADERS.index("status")] = status

    def read_form(self, row):
        return {name: self.cells[i] for i, name in enumerate(HEADERS[:34])}

    def read_flags(self, row):
        raw = self.cells[HEADERS.index("flags")]
        return [f.strip() for f in str(raw).split(",") if f.strip()]

    def write_result(self, row, scores, result):
        self.result_writes += 1
        if scores is not None:
            for i, v in enumerate(scores):
                self.cells[34 + i] = v
        for i, v in enumerate(result):
            self.cells[COL_RESULT_START - 1 + i] = v

    def get(self, name):
        return self.cells[HEADERS.index(name)]


@pytest.fixture
def sheet(monkeypatch):
    shared = SharedSheet()
    for fn in ("allocate_row", "telegram_exists", "write_form", "write_result"):
        monkeypatch.setattr(main.sheets, fn, getattr(shared, fn))
    for fn in ("read_status", "set_status", "read_form", "read_flags", "write_result"):
        monkeypatch.setattr(pipeline.sheets, fn, getattr(shared, fn))
    monkeypatch.setattr(main.recovery, "start_scanner", lambda: None)
    monkeypatch.setattr(main.recovery, "recover", lambda **kw: [])

    monkeypatch.setattr(pipeline, "score", lambda answers: (ScorerOutput(
        scores={k: 2 for k in MODEL_QKEYS},
        evidence={k: "цитата" for k in MODEL_QKEYS},
        auto_filters={"q3_senior_dm_confirmed": True, "q1_definition_ok": True},
        ai_style=25, ai_markers=[], screening_questions=["а", "б", "в"],
    ), []))

    pipeline._row_locks.clear()
    main.ratelimit._hits.clear()
    return shared


def numbers(revenue: int = 480000) -> dict:
    """Числовые поля формы: три ровных месяца и три метрики воронки."""
    data = {}
    for slot in ("m1", "m2", "m3"):
        data[f"q1_{slot}_deals"] = "4"
        data[f"q1_{slot}_revenue"] = str(revenue)
        data[f"q1_{slot}_check"] = "120000"
    data.update({
        "q4_conv_touch": "11", "q4_conv_deal": "30", "q4_cycle_days": "40",
    })
    return data


def payload(is_mobile: bool, pasted: int = 0, revenue: int = 480000):
    data = {
        "fio": "Иванов Иван Иванович",
        "telegram": "@ivanov",
        "resume_url": "https://hh.ru/resume/123",
        "pdn_consent": "1",
        "telemetry": json.dumps({
            "is_mobile": is_mobile, "tab_switches": 2, "total_time_sec": 1200,
            "fields": {f: {"length": 200, "pasted_chars": pasted,
                           "speed_cpm": 150, "backspaces": 20} for f in OPEN_FIELDS},
        }),
    }
    data.update({f: "Да" for f in STOP_FIELDS})
    data.update({f: ANSWER for f in OPEN_FIELDS})
    data.update(numbers(revenue))
    return data


def test_telemetry_flags_survive_processor_write(sheet):
    """MOBILE записан при сабмите и должен пережить запись обработчика.

    TestClient выполняет BackgroundTasks синхронно, поэтому к возврату из
    post обработка уже прошла — это полный путь анкеты, а не его половина.
    """
    with TestClient(main.app) as c:
        r = c.post("/submit", data=payload(is_mobile=True))
    assert r.status_code == 200
    assert sheet.result_writes == 1, "обработчик должен был отработать в фоне"

    final = sheet.read_flags(2)
    assert FLAG_MOBILE in final, "обработчик затёр флаг телеметрии из AS"
    assert sheet.get("status") == STATUS_DONE
    assert sheet.get("total_score") == "=ФОРМУЛА", "формула затёрта"
    assert "заполнено с телефона" in sheet.get("flags_ru")
    assert sheet.get("score_q1") == 3, "три ровных месяца — балл считает код"

    # Идемпотентность на реальном пути: повторный запуск ничего не меняет.
    pipeline.process_row(2)
    assert sheet.result_writes == 1


def test_processor_merges_own_flags_with_telemetry(sheet):
    """Свои флаги обработчик добавляет к вычитанным, а не заменяет ими."""
    # Выручка втрое выше, чем сделки на чек, — обработчик добавит MATH к
    # флагам, записанным при сабмите.
    with TestClient(main.app) as c:
        c.post("/submit", data=payload(is_mobile=True, pasted=180, revenue=1500000))

    final = set(sheet.read_flags(2))
    assert {FLAG_MOBILE, FLAG_PASTED} <= final, "флаги сабмита потеряны"
    assert FLAG_MATH in final, "флаг обработчика не добавлен"


def test_numbers_reach_the_sheet_as_one_cell(sheet):
    """Девять чисел Q1 ложатся в свою колонку строкой, читаемой человеком."""
    with TestClient(main.app) as c:
        c.post("/submit", data=payload(is_mobile=False))

    cell = sheet.get("q1_text")
    assert cell.count("/") == 6, "три месяца по три числа"
    assert "4 / 480000 / 120000" in cell
    assert cell.endswith(ANSWER), "текст кандидата идёт следом за числами"
    assert sheet.get("q4_text").startswith("Касание→встреча: 11%; ")


def test_missing_number_is_reported_on_its_field(sheet):
    """Ошибка числового поля возвращается с именем этого поля."""
    data = payload(is_mobile=False)
    data["q1_m2_deals"] = ""
    data["q4_cycle_days"] = "полтора"
    with TestClient(main.app) as c:
        r = c.post("/submit", data=data, headers={"Accept": "application/json"})

    assert r.status_code == 400
    errors = r.json()["errors"]
    assert "q1_m2_deals" in errors and "q4_cycle_days" in errors
    assert sheet.get("status") == "", "анкета с битыми числами не записывается"


def test_desktop_submit_has_no_mobile_flag(sheet):
    with TestClient(main.app) as c:
        c.post("/submit", data=payload(is_mobile=False))
    assert FLAG_MOBILE not in sheet.read_flags(2)
