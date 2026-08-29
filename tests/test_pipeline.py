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
from app.models import MODEL_QKEYS, OPEN_FIELDS, STOP_FIELDS, ScorerOutput
from tests.conftest import CLEAN_PROFILE, SHEET_STOPS

FULL_SCORES = {"q1": 3, "q2": 3, "q3": 3, "q4": 3, "q5": 3}


class FakeSheet:
    """Лист в памяти: 54 колонки, AN считается формулой и должна остаться нетронутой."""

    def __init__(self, form: dict, status=STATUS_NEW, flags="", profile=None):
        from app.profile import pack_markers

        self.cells = [""] * 54
        for name, value in form.items():
            self.cells[HEADERS.index(name)] = value
        self.cells[HEADERS.index("status")] = status
        self.cells[HEADERS.index("flags")] = flags
        # AP заполняется при сабмите — обработчик обязан вернуть ответы туда.
        self.cells[HEADERS.index("ai_markers")] = pack_markers(
            CLEAN_PROFILE if profile is None else profile, []
        )
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

    def read_profile(self, row):
        from app.profile import unpack_markers

        return unpack_markers(self.cells[HEADERS.index("ai_markers")])[1]

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
    for fn in ("read_status", "set_status", "read_form", "read_flags",
               "read_profile", "write_result"):
        monkeypatch.setattr(pipeline.sheets, fn, getattr(sheet, fn))
    pipeline._row_locks.clear()


def form_data(fixture):
    data = {f: fixture["stops"][f] for f in STOP_FIELDS}
    data.update({f: fixture["answers"][f] for f in OPEN_FIELDS})
    data["id"] = "test123"
    return data


def stub_model(monkeypatch, fixture, scores=None, fail=False):
    """Подменяет скорер — сети в тестах нет."""
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
        scores={k: scores[k] for k in MODEL_QKEYS},
        evidence={k: f"цитата {k}" for k in MODEL_QKEYS},
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
    evidence = json.loads(sheet.get("evidence_json"))
    assert evidence["q2"] == "цитата q2"
    assert evidence["q1"] == "цитата q1", "цитата нужна по всем пяти вопросам"
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


def test_profile_survives_the_result_write(monkeypatch, fixtures):
    """Обработчик перезаписывает AP целиком и обязан вернуть туда ответы.

    Признаки машинного текста и ответы кандидата живут в одной ячейке:
    отдельной колонки под ответы в раскладке нет. Забыть вычитать её —
    значит молча стереть половину анкеты.
    """
    from app.profile import unpack_markers

    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture), profile=fixture["profile"])
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)

    markers, profile = unpack_markers(sheet.get("ai_markers"))
    assert profile == fixture["profile"], "ответы кандидата затёрты"
    assert markers == ["ровные абзацы"], "признаки от модели не записаны"


def test_answers_are_readable_in_the_showcase(monkeypatch, fixtures):
    """JSON в AP раскрывать не нужно: то же самое есть фразой в flags_ru."""
    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture), profile=fixture["profile"])
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    pipeline.process_row(2)

    from app import ru

    assert ru.profile_ru(fixture["profile"]) in sheet.get("flags_ru")


def test_stop_refusal_keeps_the_answers(monkeypatch, fixtures):
    """Ранний выход по стоп-вопросам тоже проходит через запись AO..AZ."""
    from app.profile import unpack_markers

    fixture = next(f for f in fixtures if f["expected"].get("early_exit"))
    sheet = FakeSheet(form_data(fixture), profile=fixture["profile"])
    install(monkeypatch, sheet)
    monkeypatch.setattr(pipeline, "score", lambda *a, **k: pytest.fail("скорер вызван"))

    pipeline.process_row(2)

    assert unpack_markers(sheet.get("ai_markers"))[1] == fixture["profile"]


def test_scorer_failure_keeps_the_answers(monkeypatch, fixtures):
    """Ручная проверка — ровно тот случай, когда ответы нужнее всего."""
    from app.profile import unpack_markers

    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture), profile=fixture["profile"])
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture, fail=True)

    pipeline.process_row(2)

    assert sheet.get("status") == STATUS_MANUAL
    assert unpack_markers(sheet.get("ai_markers"))[1] == fixture["profile"]


def test_form_row_shape(fixtures):
    """Зона A..AH ровно 34 значения."""
    from app.models import Submission, Telemetry

    fixture = fixtures[0]
    sub = Submission(
        fio="Иванов Иван", telegram="@ivanov", resume_url="https://hh.ru/1",
        pdn_consent=True, stops=dict(SHEET_STOPS), profile=dict(CLEAN_PROFILE),
        answers=dict(fixture["answers"]),
        telemetry=Telemetry.model_validate(fixture["telemetry"]),
    )
    row = pipeline.build_form_row(sub, "abc123")
    assert len(row) == 34
    assert row[0] == "abc123"
    assert row[HEADERS.index("telegram")] == "@ivanov"
    assert row[HEADERS.index("q1_text")] == fixture["answers"]["q1_text"]
    assert COL_TOTAL_SCORE > len(row), "зона анкеты не достаёт до формулы"


def test_scorer_failure_is_never_a_refusal(monkeypatch, fixtures):
    """Сбой скорера не должен приводить к отказу.

    Ручная проверка осталась ровно одна — сбой скорера. Баллов в этом
    случае нет вообще, и вердикт принудительно B: иначе кандидат получал
    бы C за битый JSON или череду 429, то есть за чужую вину.
    """
    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture, fail=True)

    pipeline.process_row(2)

    assert sheet.get("verdict") == "B", "сбой скорера не может давать C"
    assert sheet.get("status") == STATUS_MANUAL
    assert sheet.get("decision_ru") == "Проверить руками"
    assert pipeline.SCORER_FAILED_RU in sheet.get("flags_ru")
    assert sheet.writes[0]["scores"] is None, "баллов нет — скорер не отработал"


def test_scoring_success_never_goes_to_manual(monkeypatch, fixtures):
    """Отработавший скорер всегда даёт готовый вердикт.

    Второго источника ручной проверки не осталось: числовых полей, на
    которых он раньше спотыкался, в анкете больше нет.
    """
    fixture = fixtures[0]
    sheet = FakeSheet(form_data(fixture))
    install(monkeypatch, sheet)
    stub_model(monkeypatch, fixture)

    assert pipeline.process_row(2) == STATUS_DONE
    assert sheet.get("status") == STATUS_DONE
    assert sheet.get("manual_note") == ""
