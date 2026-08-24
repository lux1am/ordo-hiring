"""Конвейер на фейковом листе: зоны записи, идемпотентность, ранний выход."""
import json

import pytest

from HEADERS import (
    COL_RESULT_END,
    COL_RESULT_START,
    COL_SCORES_END,
    COL_SCORES_START,
    COL_TOTAL_SCORE,
    HEADERS,
)
from app import pipeline
from app.config import STATUS_DONE, STATUS_MANUAL, STATUS_NEW, STATUS_PROCESSING
from app.models import ExtractorOutput, OPEN_FIELDS, STOP_FIELDS, ScorerOutput

FULL_SCORES = {"q1": 3, "q2": 3, "q3": 3, "q4": 3, "q5": 3}


class FakeSheet:
    """Лист в памяти: 54 колонки, AN считается формулой и должна остаться нетронутой."""

    def __init__(self, form: dict, status=STATUS_NEW, flags=""):
        self.cells = [""] * 54
        for name, value in form.items():
            self.cells[HEADERS.index(name)] = value
        self.cells[HEADERS.index("status")] = status
        self.cells[HEADERS.index("flags")] = flags
        self.cells[HEADERS.index("total_score")] = "=ФОРМУЛА"
        self.writes = []
        self.status_history = [status]

    # --- подменяемые функции app.sheets ---
    def read_status(self, row):
        return self.cells[HEADERS.index("status")]

    def set_status(self, row, status):
        self.cells[HEADERS.index("status")] = status
        self.status_history.append(status)

    def read_form(self, row):
        return {name: self.cells[i] for i, name in enumerate(HEADERS[:34])}

    def read_flags(self, row):
        raw = self.cells[HEADERS.index("flags")]
        return [f.strip() for f in str(raw).split(",") if f.strip()]

    def write_result(self, row, scores, result):
        self.writes.append({"scores": scores, "result": result})
        if scores is not None:
            for i, v in enumerate(scores):
                self.cells[COL_SCORES_START - 1 + i] = v
        for i, v in enumerate(result):
            self.cells[COL_RESULT_START - 1 + i] = v

    def get(self, name):
        return self.cells[HEADERS.index(name)]


def install(monkeypatch, sheet):
    for fn in ("read_status", "set_status", "read_form", "read_flags", "write_result"):
        monkeypatch.setattr(pipeline.sheets, fn, getattr(sheet, fn))
    pipeline._row_locks.clear()


def form_data(fixture):
    data = {f: fixture["stops"][f] for f in STOP_FIELDS}
    data.update({f: fixture["answers"][f] for f in OPEN_FIELDS})
    data["id"] = "test123"
    return data


def stub_model(monkeypatch, fixture, scores=None, fail=False):
    """Подменяет экстрактор и скорер — сети в тестах нет."""
    ex = ExtractorOutput.model_validate(fixture["extracted"]) if fixture.get("extracted") else None
    monkeypatch.setattr(pipeline, "extract", lambda *a, **k: ex)

    if fail:
        def boom(*a, **k):
            raise pipeline.ScoringFailed("модель недоступна")
        monkeypatch.setattr(pipeline, "score", boom)
        return

    scores = scores or fixture["expected"]["scores"]
    if scores is None:
        # Ранний выход по стоп-вопросам: до скорера дело не дойдёт.
        monkeypatch.setattr(pipeline, "score", lambda *a, **k: pytest.fail("скорер вызван"))
        return

    result = ScorerOutput(
        scores=scores,
        evidence={k: f"цитата {k}" for k in scores},
        auto_filters=fixture["auto_filters"],
        ai_style=fixture["ai_style"],
        ai_markers=["ровные абзацы"],
        screening_questions=["вопрос 1", "вопрос 2", "вопрос 3"],
    )
    monkeypatch.setattr(pipeline, "score", lambda *a, **k: (result, []))


# ------------------------------ тесты -------------------------------------

def test_fixture_end_to_end(fixture, monkeypatch):
    exp = fixture["expected"]
    sheet = FakeSheet(form_data(fixture), flags=", ".join(exp["flags"]))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture, scores=exp.get("scores"))

    pipeline.process_row(2)

    assert sheet.get("verdict") == exp["verdict"], fixture["name"]
    assert sheet.get("status") == exp["status"], fixture["name"]
    assert sheet.get("total_score") == "=ФОРМУЛА", "формула total_score затёрта"
    assert sheet.get("manual_note") == "" and sheet.get("outcome") == ""
    assert sheet.get("decision_ru"), "витрина не должна быть пустой"
    assert sheet.get("conditions_ru"), "условия должны быть заполнены всегда"


def test_stop_no_skips_model(monkeypatch, fixtures):
    """Любое «Нет» — отказ без обращения к модели."""
    fixture = next(f for f in fixtures if f["expected"].get("early_exit"))
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)

    def must_not_be_called(*a, **k):
        pytest.fail("при стоп-вопросе модель вызываться не должна")

    monkeypatch.setattr(pipeline, "extract", must_not_be_called)
    monkeypatch.setattr(pipeline, "score", must_not_be_called)

    pipeline.process_row(2)

    assert sheet.get("verdict") == "C"
    assert sheet.get("status") == STATUS_DONE
    assert sheet.get("conditions_ru") == fixture["expected"]["conditions_ru"]
    assert sheet.get("decision_ru") == "C — отказ"
    assert sheet.writes[0]["scores"] is None, "баллов быть не должно"


@pytest.mark.parametrize("status", [STATUS_DONE, STATUS_PROCESSING, STATUS_MANUAL, "duplicate"])
def test_idempotent(monkeypatch, fixtures, status):
    """Строка обрабатывается ровно один раз — повтор запрещён любым статусом кроме new."""
    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture), status=status)
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)

    assert sheet.writes == [], f"строка со статусом {status} была обработана повторно"
    assert sheet.status_history == [status], "статус не должен меняться"


def test_second_run_after_done_is_blocked(monkeypatch, fixtures):
    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)
    assert len(sheet.writes) == 1
    pipeline.process_row(2)
    assert len(sheet.writes) == 1, "повторный запуск записал результат ещё раз"


def test_scoring_failure_goes_to_manual(monkeypatch, fixtures):
    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture, fail=True)

    pipeline.process_row(2)

    assert sheet.get("status") == STATUS_MANUAL
    assert sheet.get("verdict") == "B"
    assert sheet.get("decision_ru") == "Проверить руками"
    assert pipeline.SCORER_FAILED_RU in sheet.get("flags_ru")
    assert "техническая причина" in sheet.get("flags_ru")
    assert sheet.writes[0]["scores"] is None
    assert sheet.get("total_score") == "=ФОРМУЛА"


def test_q1_without_two_full_months_goes_to_manual(monkeypatch, fixtures):
    fixture = dict(fixtures[0])
    fixture["extracted"] = {
        "months": [{"month": "март", "deals": 4, "revenue": 400000, "avg_check": 100000}],
        "conv_touch_meeting": 11, "conv_meeting_deal": 30,
    }
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)

    assert sheet.get("status") == STATUS_MANUAL
    assert sheet.get("verdict") == "B", "manual показывает B, а не пустую ячейку"
    assert sheet.get("decision_ru") == "Проверить руками"
    assert "меньше двух месяцев" in sheet.get("flags_ru")
    assert sheet.writes[0]["scores"] is not None, "баллы всё равно записываются"


def test_hard_c_survives_manual(monkeypatch, fixtures):
    """Автофильтр Q3 объявлен безусловным — ручная проверка его не смягчает."""
    fixture = dict(fixtures[0])
    fixture["auto_filters"] = {"q3_senior_dm_confirmed": False, "q1_personal_result_confirmed": True}
    fixture["extracted"] = {"months": [], "conv_touch_meeting": None, "conv_meeting_deal": None}
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)

    assert sheet.get("status") == STATUS_MANUAL
    assert sheet.get("verdict") == "C", "жёсткий C не должен подменяться на B"


def test_result_zone_shape(monkeypatch, fixtures):
    """Зона AO..AZ ровно 12 значений и в порядке HEADERS."""
    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)
    write = sheet.writes[0]

    assert len(write["result"]) == COL_RESULT_END - COL_RESULT_START + 1 == 12
    assert len(write["scores"]) == COL_SCORES_END - COL_SCORES_START + 1 == 5
    assert sheet.get("notified") == "", "notified — заглушка, код её не заполняет"
    assert json.loads(sheet.get("evidence_json"))["q1"] == "цитата q1"
    assert sheet.get("screening_questions").count("\n") == 2, "три вопроса на интервью"


def test_telemetry_flag_adds_screening_question(monkeypatch, fixtures):
    """Вставка не понижает вердикт, а добавляет четвёртый вопрос на интервью.

    Флаг при этом никуда не девается: в таблице он по-прежнему виден и в
    коде флагов, и в русской строке замечаний.
    """
    from app.verdict import TELEMETRY_SCREENING_RU

    fixture = next(f for f in fixtures if f["expected"]["scores"] == FULL_SCORES)
    sheet = FakeSheet(form_data(fixture), flags="PASTED")
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)

    questions = sheet.get("screening_questions").split("\n")
    assert len(questions) == 4
    assert questions[-1] == TELEMETRY_SCREENING_RU
    assert sheet.get("verdict") == "A", "телеметрия вердикт не понижает"
    assert "PASTED" in sheet.get("flags")
    assert "вставленный текст" in sheet.get("flags_ru")


def test_form_row_shape(fixtures):
    """Зона A..AH ровно 34 значения."""
    from app.models import Submission, Telemetry

    fixture = fixtures[0]
    sub = Submission(
        fio="Иванов Иван", telegram="@ivanov", resume_url="https://hh.ru/1",
        pdn_consent=True, stops=fixture["stops"], answers=fixture["answers"],
        telemetry=Telemetry.model_validate(fixture["telemetry"]),
    )
    row = pipeline.build_form_row(sub, "abc123")
    assert len(row) == 34
    assert row[0] == "abc123"
    assert row[HEADERS.index("telegram")] == "@ivanov"
    assert row[HEADERS.index("q1_text")] == fixture["answers"]["q1_text"]
    assert COL_TOTAL_SCORE > len(row), "зона анкеты не достаёт до формулы"


def test_scorer_failure_never_produces_hard_c(monkeypatch, fixtures):
    """Сбой скорера не должен приводить к отказу.

    Два источника manual разведены: при manual от экстрактора скорер
    отработал и автофильтр по вопросу 3 применяется, а при сбое самого
    скорера данных по вопросу 3 нет вообще — вердикт принудительно B.
    Иначе кандидат получал бы C за битый JSON или череду 429.
    """
    # Берём кандидата, который при живом скорере получил бы жёсткий C.
    fixture = next(f for f in fixtures if not f["auto_filters"]["q3_senior_dm_confirmed"])
    assert fixture["expected"]["verdict"] == "C", "фикстура должна давать C при живом скорере"

    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture, fail=True)

    pipeline.process_row(2)

    assert sheet.get("verdict") == "B", "сбой скорера не может давать C"
    assert sheet.get("status") == STATUS_MANUAL
    assert sheet.get("decision_ru") == "Проверить руками"
    assert pipeline.SCORER_FAILED_RU in sheet.get("flags_ru")
    assert sheet.writes[0]["scores"] is None, "баллов нет — скорер не отработал"


@pytest.mark.parametrize("q3_confirmed", [True, False])
def test_extractor_manual_still_scores(monkeypatch, fixtures, q3_confirmed):
    """Manual от экстрактора не обрывает конвейер: баллы пишутся в любом случае."""
    fixture = dict(fixtures[0])
    fixture["auto_filters"] = {
        "q3_senior_dm_confirmed": q3_confirmed,
        "q1_personal_result_confirmed": True,
    }
    # Один подтверждённый месяц вместо двух — manual от экстрактора.
    fixture["extracted"] = {
        "months": [{"month": "март", "deals": 4, "revenue": 400000, "avg_check": 100000}],
        "conv_touch_meeting": 11, "conv_meeting_deal": 30,
    }
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)

    assert sheet.get("status") == STATUS_MANUAL
    assert sheet.writes[0]["scores"] == [3, 3, 3, 3, 3], "баллы нужны и при manual"
    for col in ("score_q1", "score_q5"):
        assert sheet.get(col) == 3
    # Скорер отработал, значит автофильтр по вопросу 3 в силе.
    assert sheet.get("verdict") == ("B" if q3_confirmed else "C")
