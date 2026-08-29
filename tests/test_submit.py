"""Сквозной путь POST /submit на подменённом листе."""
import json

import pytest
from fastapi.testclient import TestClient

from HEADERS import HEADERS
from app import main
from app.config import HONEYPOT_FIELD, STATUS_DUPLICATE, STATUS_NEW
from app.models import OPEN_FIELDS
from tests.conftest import choice_fields

# Длиннее самого строгого минимума — 80 знаков у вопросов 1 и 4.
ANSWER = ("Разберу, на каком шаге срываются: если отказ идёт сразу после "
          "приветствия, дело в первой фразе. Поменяю её и добью норму дня.")
# Третий вопрос жёстко ограничен семьюдесятью знаками.
SHORT_ANSWER = "Иван, за 15 минут покажу, как снизить цену заявки. Когда удобно?"


class FakeSheets:
    def __init__(self):
        self.rows = {}
        self.results = {}
        self.next_row = 2
        self.known_telegrams = set()

    def allocate_row(self):
        row = self.next_row
        self.next_row += 1
        return row

    def write_form(self, row, values, flags, status=STATUS_NEW, markers=""):
        self.rows[row] = {
            "values": values, "flags": flags, "status": status, "markers": markers,
        }

    def write_result(self, row, scores, result):
        self.results[row] = {"scores": scores, "result": result}

    def telegram_exists(self, telegram):
        return telegram.lstrip("@").lower() in self.known_telegrams


@pytest.fixture
def sheet(monkeypatch):
    fake = FakeSheets()
    for fn in ("allocate_row", "write_form", "write_result", "telegram_exists"):
        monkeypatch.setattr(main.sheets, fn, getattr(fake, fn))
    monkeypatch.setattr(main, "process_row", lambda row: fake.__setattr__("processed", row))
    monkeypatch.setattr(main.recovery, "recover", lambda **kw: [])
    monkeypatch.setattr(main.recovery, "start_scanner", lambda: None)
    main.ratelimit._hits.clear()
    return fake


def payload(**over):
    data = {
        "fio": "Иванов Иван Иванович",
        "telegram": "@ivanov",
        "resume_url": "https://hh.ru/resume/123",
        "pdn_consent": "1",
        "telemetry": json.dumps({
            "is_mobile": False, "tab_switches": 1, "total_time_sec": 900,
            "fields": {f: {"length": 120, "pasted_chars": 0, "speed_cpm": 150,
                           "backspaces": 15} for f in OPEN_FIELDS},
        }),
    }
    data.update(choice_fields())
    data.update({f: ANSWER for f in OPEN_FIELDS})
    data["q3_text"] = SHORT_ANSWER
    data.update(over)
    return data


def test_accepted_and_queued(sheet):
    with TestClient(main.app) as c:
        r = c.post("/submit", data=payload())

    assert r.status_code == 200
    assert "анкета отправлена" in r.text.lower()

    row = sheet.rows[2]
    assert row["status"] == STATUS_NEW
    assert len(row["values"]) == 34, "зона анкеты — ровно 34 значения"
    assert row["values"][HEADERS.index("telegram")] == "@ivanov"
    assert row["values"][HEADERS.index("fio")] == "Иванов Иван Иванович"
    assert row["values"][HEADERS.index("pdn_consent")] == "Да"
    assert sheet.processed == 2, "обработка должна быть поставлена в очередь"


def test_honeypot_is_silently_dropped(sheet):
    """Бот получает обычный ответ, но строку не занимает."""
    with TestClient(main.app) as c:
        r = c.post("/submit", data=payload(**{HONEYPOT_FIELD: "http://spam.example"}))

    assert r.status_code == 200
    assert "анкета отправлена" in r.text.lower()
    assert sheet.rows == {}, "приманка не должна расходовать строки таблицы"


def test_duplicate_telegram(sheet):
    sheet.known_telegrams.add("ivanov")
    with TestClient(main.app) as c:
        r = c.post("/submit", data=payload())

    assert r.status_code == 200
    assert sheet.rows[2]["status"] == STATUS_DUPLICATE
    result = sheet.results[2]["result"]
    assert result[HEADERS.index("status") - 40] == STATUS_DUPLICATE
    assert sheet.results[2]["scores"] is None, "модель для дубля не вызывается"
    assert not hasattr(sheet, "processed"), "дубль не ставится в обработку"


def test_rate_limit(sheet):
    with TestClient(main.app) as c:
        codes = [
            c.post("/submit", data=payload(telegram=f"@user{i}"),
                   headers={"x-forwarded-for": "9.9.9.9"}).status_code
            for i in range(7)
        ]
    assert codes[:5] == [200] * 5
    assert codes[5:] == [429, 429]


def test_rate_limit_is_per_address(sheet):
    with TestClient(main.app) as c:
        for i in range(5):
            c.post("/submit", data=payload(telegram=f"@a{i}"),
                   headers={"x-forwarded-for": "1.1.1.1"})
        r = c.post("/submit", data=payload(telegram="@other"),
                   headers={"x-forwarded-for": "2.2.2.2"})
    assert r.status_code == 200


def test_broken_telemetry_does_not_reject_form(sheet):
    """Кривая телеметрия не должна стоить кандидату анкеты."""
    with TestClient(main.app) as c:
        r = c.post("/submit", data=payload(telemetry="{не json"))
    assert r.status_code == 200
    assert 2 in sheet.rows


def test_sheets_failure_shows_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Google API недоступен")

    monkeypatch.setattr(main.sheets, "telegram_exists", lambda tg: False)
    monkeypatch.setattr(main.sheets, "allocate_row", boom)
    monkeypatch.setattr(main.recovery, "start_scanner", lambda: None)
    main.ratelimit._hits.clear()

    with TestClient(main.app) as c:
        r = c.post("/submit", data=payload())

    assert r.status_code == 500
    assert "Не удалось сохранить" in r.text
    assert "Иванов Иван Иванович" in r.text, "введённое не должно потеряться"
