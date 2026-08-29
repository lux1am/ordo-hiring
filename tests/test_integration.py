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
from app.checks import FLAG_MOBILE, FLAG_PASTED, FLAG_STABILITY_FIRST
from app.config import STATUS_DONE, STATUS_NEW
from app.models import MODEL_QKEYS, OPEN_FIELDS, ScorerOutput
from app.profile import unpack_markers
from tests.conftest import CLEAN_PROFILE, choice_fields

# Длиннее самого строгого минимума — 80 знаков у вопросов 1 и 4.
ANSWER = ("Разберу, на каком шаге срываются: если отказ идёт сразу после "
          "приветствия, дело в первой фразе. Поменяю её и добью норму дня.")
# Третий вопрос жёстко ограничен семьюдесятью знаками.
SHORT_ANSWER = "Иван, за 15 минут покажу, как снизить цену заявки. Когда удобно?"


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

    def write_form(self, row, values, flags, status=STATUS_NEW, markers=""):
        for i, v in enumerate(values):
            self.cells[i] = v
        self.cells[HEADERS.index("ai_markers")] = markers
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

    def read_profile(self, row):
        return unpack_markers(self.cells[HEADERS.index("ai_markers")])[1]

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
    for fn in ("read_status", "set_status", "read_form", "read_flags",
               "read_profile", "write_result"):
        monkeypatch.setattr(pipeline.sheets, fn, getattr(shared, fn))
    monkeypatch.setattr(main.recovery, "start_scanner", lambda: None)
    monkeypatch.setattr(main.recovery, "recover", lambda **kw: [])

    monkeypatch.setattr(pipeline, "score", lambda answers: (ScorerOutput(
        scores={k: 2 for k in MODEL_QKEYS},
        evidence={k: "цитата" for k in MODEL_QKEYS},
        ai_style=25, ai_markers=[], screening_questions=["а", "б", "в"],
    ), []))

    pipeline._row_locks.clear()
    main.ratelimit._hits.clear()
    return shared


def payload(is_mobile: bool, pasted: int = 0, **over):
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
    data.update(choice_fields(**over))
    data.update({f: ANSWER for f in OPEN_FIELDS})
    data["q3_text"] = SHORT_ANSWER
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
    assert sheet.get("score_q1") == 2, "балл ставит модель, здесь она подменена"

    # Идемпотентность на реальном пути: повторный запуск ничего не меняет.
    pipeline.process_row(2)
    assert sheet.result_writes == 1


def test_profile_flags_are_written_at_submit(sheet):
    """Флаги вопросов о мотивации ложатся в AS вместе с телеметрией.

    Отдельных колонок под эти ответы в листе нет: раскладка зафиксирована на
    54 колонки, и в таблицу попадает не сам ответ, а то, что из него
    следует, — код флага и русская строка замечаний.
    """
    with TestClient(main.app) as c:
        c.post("/submit", data=payload(
            is_mobile=True, pasted=180,
            flag_rank_stability="1", flag_rank_income="2",
            flag_rank_schedule="3", flag_rank_growth="4",
        ))

    final = set(sheet.read_flags(2))
    assert {FLAG_MOBILE, FLAG_PASTED} <= final, "флаги телеметрии потеряны"
    assert FLAG_STABILITY_FIRST in final, "флаг вопроса о мотивации не записан"
    assert "стабильность важнее дохода" in sheet.get("flags_ru")
    assert sheet.get("verdict") == "B", "флаг понижает вердикт, а не только виден"


def test_answers_reach_the_sheet_and_survive_processing(sheet):
    """Полный путь ответов о мотивации: форма → AP → обработчик → AP.

    Отдельной колонки под них нет, поэтому они едут в ai_markers вместе с
    признаками машинного текста, и обработчик обязан вернуть их обратно.
    """
    with TestClient(main.app) as c:
        c.post("/submit", data=payload(is_mobile=False))

    markers, profile = unpack_markers(sheet.get("ai_markers"))
    assert profile == CLEAN_PROFILE, "ответы кандидата не доехали до таблицы"
    assert markers == [], "модель в этом тесте подменена и признаков не даёт"

    # То же самое видно в витрине без раскрытия JSON.
    assert "цель — РОП" in sheet.get("flags_ru")
    assert "приоритет: доход, график, рост, стабильность" in sheet.get("flags_ru")


def test_own_wording_reaches_the_sheet(sheet):
    """Текст из «другое» — единственный свободный ответ среди вариантов."""
    data = payload(is_mobile=False)
    data["flag_motive"] = "Другое"
    data["flag_motive_other"] = "Хочу работать голосом"
    with TestClient(main.app) as c:
        c.post("/submit", data=data)

    profile = unpack_markers(sheet.get("ai_markers"))[1]
    assert profile["flag_motive_other"] == "Хочу работать голосом"
    assert "мотивация: хочу работать голосом" in sheet.get("flags_ru")


def test_timezone_lands_in_its_question_cell(sheet):
    """Часовой пояс приписан к ячейке своего стоп-вопроса."""
    with TestClient(main.app) as c:
        c.post("/submit", data=payload(is_mobile=False))

    assert sheet.get("stop_3_svyaz") == "Да — Ваш часовой пояс: МСК"
    assert sheet.get("conditions_ru") == "Все условия приняты"


def test_failing_stop_answer_is_a_refusal(sheet):
    """Непроходной вариант — отказ, и модель для него не вызывается."""
    with TestClient(main.app) as c:
        c.post("/submit", data=payload(is_mobile=False, stop_2_chasy="10–14"))

    assert sheet.get("verdict") == "C"
    assert sheet.get("status") == STATUS_DONE
    assert "15 и больше часов в неделю" in sheet.get("conditions_ru")


def test_choice_error_is_reported_on_its_field(sheet):
    """Ошибка варианта возвращается с именем своего вопроса."""
    data = payload(is_mobile=False, flag_rank_growth="1")
    data["stop_4_oformlenie"] = ""
    with TestClient(main.app) as c:
        r = c.post("/submit", data=data, headers={"Accept": "application/json"})

    assert r.status_code == 400
    errors = r.json()["errors"]
    assert "stop_4_oformlenie" in errors and "flag_rank" in errors
    assert sheet.get("status") == "", "анкету с битыми ответами не записываем"


def test_multi_choice_survives_the_form(sheet):
    """Два выбранных варианта доходят до флагов, а не схлопываются в один."""
    data = payload(is_mobile=False)
    data["flag_motive"] = ["Уйти на удалёнку", "Сменить сферу"]
    with TestClient(main.app) as c:
        c.post("/submit", data=data)

    assert "MOTIVATION_AVOIDANCE" not in sheet.read_flags(2), (
        "два мотива — это уже не бегство, значит пришли оба"
    )


def test_desktop_submit_has_no_mobile_flag(sheet):
    with TestClient(main.app) as c:
        c.post("/submit", data=payload(is_mobile=False))
    assert FLAG_MOBILE not in sheet.read_flags(2)
